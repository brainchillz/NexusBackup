package PVE::Storage::Custom::HvbackupPlugin;

# hvbackup storage plug-in for Proxmox VE 9: registers the backup server as a storage of
# type 'hvbackup' with the 'backup-provider' feature. Listing, pruning and restoring talk to
# the server; the data path lives in PVE::BackupProvider::Plugin::Hvbackup and the Python
# helper /opt/hvbackup/pve_helper.py. Installed by the server's /api/v1/pve/install.sh.

use strict;
use warnings;

use JSON;
use PVE::Tools qw(run_command file_set_contents file_get_contents);
use PVE::JSONSchema;
use PVE::Storage::Plugin;
use PVE::BackupProvider::Plugin::Hvbackup;

use base qw(PVE::Storage::Plugin);

sub api {
    return 15;
}

sub type {
    return 'hvbackup';
}

sub plugindata {
    return {
        content => [{ backup => 1, none => 1 }, { backup => 1 }],
        features => { 'backup-provider' => 1 },
        'sensitive-properties' => { password => 1 },
    };
}

sub properties {
    return {};
}

sub options {
    return {
        server => { fixed => 1 },
        port => { optional => 1 },
        password => { optional => 1 },
        fingerprint => { optional => 1 },
        nodes => { optional => 1 },
        disable => { optional => 1 },
        content => { optional => 1 },
        'prune-backups' => { optional => 1 },
        'max-protected-backups' => { optional => 1 },
        bwlimit => { optional => 1 },
    };
}

# ── token kept in /etc/pve/priv like the PBS password ───────────────
sub token_file_name {
    my ($scfg, $storeid) = @_;
    return "/etc/pve/priv/storage/${storeid}.pw";
}

sub set_token {
    my ($scfg, $storeid, $token) = @_;
    my $file = token_file_name($scfg, $storeid);
    mkdir "/etc/pve/priv/storage";
    PVE::Tools::file_set_contents($file, "$token\n");
}

sub delete_token {
    my ($scfg, $storeid) = @_;
    my $file = token_file_name($scfg, $storeid);
    if (!unlink $file) {
        return if $! == POSIX::ENOENT();
        die "failed to delete token! $!\n";
    }
}

sub get_token {
    my ($scfg, $storeid) = @_;
    my $file = token_file_name($scfg, $storeid);
    return PVE::Tools::file_read_firstline($file);
}

sub on_add_hook {
    my ($class, $storeid, $scfg, %param) = @_;
    if (defined(my $token = $param{password})) {
        set_token($scfg, $storeid, $token);
    } else {
        delete_token($scfg, $storeid);
    }
    return {};
}

sub on_update_hook {
    my ($class, $storeid, $scfg, %param) = @_;
    if (exists($param{password})) {
        if (defined($param{password})) {
            set_token($scfg, $storeid, $param{password});
        } else {
            delete_token($scfg, $storeid);
        }
    }
    return {};
}

sub on_delete_hook {
    my ($class, $storeid, $scfg) = @_;
    delete_token($scfg, $storeid);
    return;
}

# ── helper process ───────────────────────────────────────────────────
# vzdump runs with taint checks on: everything handed to exec() has to pass through a regex.
sub untaint {
    my ($v) = @_;
    return '' if !defined($v);
    $v =~ m/^([A-Za-z0-9_.:\/\-@=+%, ]*)$/ or die "hvbackup: refusing unexpected characters in '$v'\n";
    return $1;
}

sub helper_env {
    my ($scfg, $storeid) = @_;
    my $port = $scfg->{port} // 8460;
    my $env = {
        HVB_URL => "https://$scfg->{server}:$port",
        HVB_PROXY_TOKEN => get_token($scfg, $storeid) // '',
        HVB_INSECURE => '1',
        HVB_STOREID => $storeid,
    };
    $env->{HVB_FINGERPRINT} = $scfg->{fingerprint} if $scfg->{fingerprint};
    return $env;
}

# Run the Python helper and decode its JSON result. $input is written to stdin when given.
sub helper {
    my ($scfg, $storeid, $args, $input, $timeout) = @_;
    my $env = helper_env($scfg, $storeid);
    local %ENV = (%ENV, map { $_ => untaint($env->{$_}) } keys %$env);
    $ENV{PATH} = '/usr/local/bin:/usr/bin:/bin';
    my $out = '';
    my $err = '';
    my $cmd = ['/opt/hvbackup/venv/bin/python', '/opt/hvbackup/pve_helper.py', map { untaint($_) } @$args];
    eval {
        run_command(
            $cmd,
            input => $input,
            outfunc => sub { $out .= "$_[0]\n" },
            errfunc => sub { $err .= "$_[0]\n" },
            timeout => $timeout // 300,
        );
    };
    if (my $e = $@) {
        chomp $err;
        die "hvbackup helper failed: $e" . ($err ? " ($err)" : '') . "\n";
    }
    my $data = eval { decode_json($out) };
    die "hvbackup helper returned no JSON: $out $err\n" if $@;
    die "hvbackup: $data->{error}\n" if ref($data) eq 'HASH' && $data->{error};
    return $data;
}

# ── volumes ──────────────────────────────────────────────────────────
sub parse_volname {
    my ($class, $volname) = @_;
    if ($volname =~ m!^backup/(vm|ct)/(\d+)/(\d+)$!) {
        my ($btype, $vmid, $run) = ($1, $2, $3);
        return ('backup', "$btype/$vmid/$run", $vmid, undef, undef, undef, "hvb-$btype");
    }
    die "unable to parse hvbackup volume name '$volname'\n";
}

sub path {
    my ($class, $scfg, $volname, $storeid, $snapname) = @_;
    die "volume snapshot is not possible on hvbackup storage\n" if defined($snapname);
    my ($vtype, $name, $vmid) = $class->parse_volname($volname);
    # artificial URL like the PBS plug-in: archives are restored through the backup provider API
    return ("hvbackup://$scfg->{server}/$name", $vmid, $vtype);
}

sub create_base {
    die "cannot create base images on hvbackup storage\n";
}

sub clone_image {
    die "cannot clone images on hvbackup storage\n";
}

sub alloc_image {
    die "cannot allocate images on hvbackup storage\n";
}

sub free_image {
    my ($class, $storeid, $scfg, $volname, $isBase) = @_;
    my (undef, $name) = $class->parse_volname($volname);
    my ($btype, $vmid, $run) = split('/', $name);
    helper($scfg, $storeid, ['delete', '--run', $run]);
    return;
}

sub list_images {
    return [];
}

sub list_volumes {
    my ($class, $storeid, $scfg, $vmid, $content_types) = @_;
    my $res = [];
    return $res if !grep { $_ eq 'backup' } @$content_types;
    my $args = ['archives'];
    push @$args, '--vmid', $vmid if defined($vmid);
    my $data = helper($scfg, $storeid, $args, undef, 60);
    for my $a (@{ $data->{archives} // [] }) {
        push @$res,
            {
                volid => "$storeid:$a->{volname}",
                format => "hvb-" . ($a->{vmtype} eq 'qemu' ? 'vm' : 'ct'),
                size => $a->{size} // 1,
                content => 'backup',
                vmid => int($a->{vmid}),
                ctime => int($a->{ctime}),
                subtype => $a->{vmtype},
                notes => $a->{notes},
            };
    }
    return $res;
}

sub status {
    my ($class, $storeid, $scfg, $cache) = @_;
    my $st = eval { helper($scfg, $storeid, ['status'], undef, 30) };
    return (0, 0, 0, 0) if !$st;
    return ($st->{total}, $st->{avail}, $st->{used}, 1);
}

sub activate_storage {
    my ($class, $storeid, $scfg, $cache) = @_;
    return 1;
}

sub deactivate_storage {
    return 1;
}

sub activate_volume {
    return 1;
}

sub deactivate_volume {
    return 1;
}

sub check_connection {
    my ($class, $storeid, $scfg) = @_;
    my $st = eval { helper($scfg, $storeid, ['status'], undef, 15) };
    return $st ? 1 : 0;
}

sub volume_size_info {
    my ($class, $scfg, $storeid, $volname, $timeout) = @_;
    my (undef, $name, $vmid) = $class->parse_volname($volname);
    for my $v (@{ $class->list_volumes($storeid, $scfg, $vmid, ['backup']) }) {
        return $v->{size} if $v->{volid} eq "$storeid:$volname";
    }
    return 0;
}

sub volume_resize {
    die "volume resize is not possible on hvbackup storage\n";
}

sub volume_snapshot {
    die "volume snapshot is not possible on hvbackup storage\n";
}

sub volume_snapshot_rollback {
    die "volume snapshot rollback is not possible on hvbackup storage\n";
}

sub volume_snapshot_delete {
    die "volume snapshot delete is not possible on hvbackup storage\n";
}

sub volume_has_feature {
    return 0;
}

sub get_volume_notes {
    my ($class, $scfg, $storeid, $volname) = @_;
    my (undef, $name, $vmid) = $class->parse_volname($volname);
    for my $v (@{ $class->list_volumes($storeid, $scfg, $vmid, ['backup']) }) {
        return $v->{notes} if $v->{volid} eq "$storeid:$volname";
    }
    return '';
}

sub update_volume_notes {
    die "volume notes are kept on the hvbackup server\n";
}

sub get_volume_attribute {
    my ($class, $scfg, $storeid, $volname, $attribute) = @_;
    return $class->get_volume_notes($scfg, $storeid, $volname) if $attribute eq 'notes';
    return 0 if $attribute eq 'protected';
    return;
}

sub update_volume_attribute {
    my ($class, $scfg, $storeid, $volname, $attribute, $value) = @_;
    die "attribute '$attribute' is not supported on hvbackup storage\n";
}

sub get_identity {
    my ($class, $scfg, $storeid) = @_;
    my $st = helper($scfg, $storeid, ['status'], undef, 30);
    return "hvbackup:" . ($st->{repo_id} // $scfg->{server});
}

sub new_backup_provider {
    my ($class, $scfg, $storeid, $log_function) = @_;
    return PVE::BackupProvider::Plugin::Hvbackup->new($class, $scfg, $storeid, $log_function);
}

1;
