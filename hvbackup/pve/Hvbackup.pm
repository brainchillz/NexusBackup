package PVE::BackupProvider::Plugin::Hvbackup;

# hvbackup backup provider for Proxmox VE 9. Thin by design: Proxmox hands us a snapshot
# access to each disk (file-handle mechanism) and the list of dirty regions; the Python
# helper reads, hashes and uploads the chunks the server does not have, and the server
# merges the result into a restore point. Containers are streamed as a tar through the
# same path. Restores serve each disk from the repository over NBD for qemu-img, and
# containers as a tar file.

use strict;
use warnings;

use Fcntl qw(F_GETFD F_SETFD FD_CLOEXEC);
use File::Path qw(make_path remove_tree);
use JSON;
use POSIX qw(:sys_wait_h);
use Time::HiRes qw(usleep);

use PVE::Tools qw(run_command file_set_contents file_get_contents);
use PVE::Storage::Common;
require PVE::Storage::Custom::HvbackupPlugin;

use base qw(PVE::BackupProvider::Plugin::Base);

my $RUN_DIR = '/run/hvbackup';
my $TMP_DIR = '/var/tmp/hvbackup';
my $HELPER = ['/opt/hvbackup/venv/bin/python', '/opt/hvbackup/pve_helper.py'];

sub new {
    my ($class, $storage_plugin, $scfg, $storeid, $log_function) = @_;
    my $self = bless {
        scfg => $scfg,
        storeid => $storeid,
        'storage-plugin' => $storage_plugin,
        'log-function' => $log_function,
        runs => {},
        restores => {},
    }, $class;
    return $self;
}

sub log_info { my ($self, $m) = @_; $self->{'log-function'}->('info', $m); }
sub log_warn { my ($self, $m) = @_; $self->{'log-function'}->('warn', $m); }

sub env {
    my ($self) = @_;
    # backup_container() runs forked as the container's unprivileged root, which cannot read
    # /etc/pve/priv: backup_container_prepare() caches the environment while still privileged
    return $self->{'env-cache'} if $self->{'env-cache'};
    return PVE::Storage::Custom::HvbackupPlugin::helper_env($self->{scfg}, $self->{storeid});
}

# Run the helper; capture its JSON (last stdout line); forward 'progress ...' stderr lines to the task log.
sub helper {
    my ($self, $args, $opts) = @_;
    my $env = { %{ $self->env() }, %{ $opts->{env} // {} } };
    local %ENV = (%ENV, map { $_ => PVE::Storage::Custom::HvbackupPlugin::untaint($env->{$_}) } keys %$env);
    $ENV{PATH} = '/usr/local/bin:/usr/bin:/bin';
    my $out = '';
    my $err = '';
    my @cmd = (@$HELPER, map { PVE::Storage::Custom::HvbackupPlugin::untaint($_) } @$args);
    eval {
        run_command(
            [@cmd],
            input => $opts->{input},
            outfunc => sub { $out .= "$_[0]\n" },
            errfunc => sub {
                my ($line) = @_;
                if ($line =~ /^(progress|info|warn) (.*)$/) {
                    my ($lvl, $msg) = ($1, $2);
                    $self->log_info($msg) if $lvl ne 'progress' || $opts->{'log-progress'};
                } else {
                    $err .= "$line\n";
                }
            },
            timeout => $opts->{timeout} // 86400,
            keeplocale => 1,
        );
    };
    if (my $e = $@) {
        chomp $err;
        die "hvbackup helper failed: $e" . ($err ? " ($err)" : '') . "\n";
    }
    my ($last) = ($out =~ /(\{.*\})\s*$/s);
    my $data = eval { decode_json($last // '') };
    die "hvbackup helper returned no JSON: $out $err\n" if $@ || !$data;
    die "hvbackup: $data->{error}\n" if ref($data) eq 'HASH' && $data->{error};
    return $data;
}

sub provider_name {
    return 'hvbackup';
}

# ── job ──────────────────────────────────────────────────────────────
sub job_init {
    my ($self, $start_time) = @_;
    my $st = $self->helper(['status'], { timeout => 60 });
    $self->log_info("hvbackup server reachable, repository $st->{repo_id}, " . int(($st->{avail} // 0) / 2**30) . " GiB free");
    return;
}

sub job_cleanup {
    return;
}

# ── per-guest backup ─────────────────────────────────────────────────
sub backup_init {
    my ($self, $vmid, $vmtype, $start_time) = @_;
    my $node = PVE::INotify::nodename();
    my $res = $self->helper(['init', '--vmid', $vmid, '--vmtype', $vmtype, '--node', $node, '--start-time', $start_time], { timeout => 300 });
    $self->{runs}->{$vmid} = { run => $res->{run_id}, vmtype => $vmtype, archive => $res->{archive_name}, 'bytes-read' => 0, 'bytes-uploaded' => 0, 'chunks-new' => 0 };
    $self->log_info("hvbackup run $res->{run_id} for $res->{vm_name}, archive $res->{archive_name}");
    return { 'archive-name' => $res->{archive_name} };
}

sub backup_cleanup {
    my ($self, $vmid, $vmtype, $success, $info) = @_;
    my $r = $self->{runs}->{$vmid} or return {};
    my $body = {
        success => $success ? JSON::true : JSON::false,
        error => $info->{error},
        guest_config => $r->{'guest-config'},
        firewall_config => $r->{'firewall-config'},
        bytes_read => $r->{'bytes-read'},
        bytes_uploaded => $r->{'bytes-uploaded'},
        chunks_new => $r->{'chunks-new'},
    };
    my $res = eval { $self->helper(['finish', '--run', $r->{run}], { input => encode_json($body), timeout => 600 }) };
    if (my $e = $@) {
        delete $self->{runs}->{$vmid};
        die $e if $success;
        $self->log_warn("could not report the failure to the server: $e");
        return {};
    }
    if ($success) {
        $self->log_info("restore point $res->{restore_point} written on the server ($res->{kind})");
    }
    # the per-container credential file, if any
    unlink "$RUN_DIR/ct-$vmid.env";
    delete $self->{'env-cache'};
    delete $self->{runs}->{$vmid};
    return { stats => { 'archive-size' => $res->{archive_size} // 0 } };
}

sub backup_get_mechanism {
    my ($self, $vmid, $vmtype) = @_;
    return 'file-handle' if $vmtype eq 'qemu';
    return 'directory' if $vmtype eq 'lxc';
    die "unsupported guest type '$vmtype'\n";
}

sub backup_handle_log_file {
    my ($self, $vmid, $filename) = @_;
    my $r = $self->{runs}->{$vmid} or return;
    my $text = eval { file_get_contents($filename) } // '';
    eval { $self->helper(['log', '--run', $r->{run}], { input => $text, timeout => 60 }) };
    $self->log_warn("could not upload the task log: $@") if $@;
    return;
}

sub backup_vm_query_incremental {
    my ($self, $vmid, $volumes) = @_;
    my $r = $self->{runs}->{$vmid} or die "backup_init was not called for $vmid\n";
    my $sizes = { map { $_ => { size => $volumes->{$_}->{size} } } keys %$volumes };
    my $res = $self->helper(['incremental', '--run', $r->{run}], { input => encode_json({ volumes => $sizes }), timeout => 120 });
    return { map { $_ => $res->{$_} } grep { defined $res->{$_} } keys %$volumes };
}

sub backup_vm {
    my ($self, $vmid, $guest_config, $volumes, $info) = @_;
    my $r = $self->{runs}->{$vmid} or die "backup_init was not called for $vmid\n";
    $r->{'guest-config'} = $guest_config;
    $r->{'firewall-config'} = $info->{'firewall-config'};

    for my $device (sort keys %$volumes) {
        my $v = $volumes->{$device};
        my $size = $v->{size};
        my $mode = $v->{'bitmap-mode'} // 'none';
        my $fh = $v->{'file-handle'};
        my $next = $v->{'next-dirty-region'};

        # collect the regions first (the closure reads nbdinfo lazily)
        my @regions;
        my $dirty = 0;
        while (1) {
            my ($off, $len) = $next->();
            last if !defined($off);
            push @regions, [int($off), int($len)];
            $dirty += $len;
        }
        $self->log_info(sprintf("%s: %s, %.0f MiB in %d region(s) to read", $device, $mode eq 'reuse' ? 'incremental (bitmap)' : 'full', $dirty / 2**20, scalar(@regions)));
        if (!@regions) {
            $self->helper(['volume', '--run', $r->{run}, '--dev', $device, '--size', $size, '--bitmap-mode', $mode, '--fd', '-1'],
                { input => encode_json({ regions => [] }), timeout => 600 });
            next;
        }

        # hand the snapshot access to the helper: clear close-on-exec on the fd
        my $flags = fcntl($fh, F_GETFD, 0) // die "fcntl F_GETFD failed: $!\n";
        fcntl($fh, F_SETFD, $flags & ~FD_CLOEXEC) // die "fcntl F_SETFD failed: $!\n";
        my $fd = fileno($fh);
        my $path = readlink("/proc/$$/fd/$fd") // '';

        my $res = $self->helper(
            ['volume', '--run', $r->{run}, '--dev', $device, '--size', $size, '--bitmap-mode', $mode, '--fd', $fd, '--path', $path,
             ($info->{'bandwidth-limit'} ? ('--bwlimit', $info->{'bandwidth-limit'}) : ())],
            { input => encode_json({ regions => \@regions }), 'log-progress' => 1 },
        );
        fcntl($fh, F_SETFD, $flags);
        $r->{'bytes-read'} += $res->{bytes_read} // 0;
        $r->{'bytes-uploaded'} += $res->{bytes_uploaded} // 0;
        $r->{'chunks-new'} += $res->{chunks_new} // 0;
        $self->log_info(sprintf("%s: %.0f MiB read, %d chunks new, %.0f MiB uploaded in %.0fs", $device, ($res->{bytes_read} // 0) / 2**20,
            $res->{chunks_new} // 0, ($res->{bytes_uploaded} // 0) / 2**20, $res->{seconds} // 0));

        # free the fleecing space for what we have read
        for my $reg (@regions) {
            eval { PVE::Storage::Common::deallocate($fh, $reg->[0], $reg->[1]) };
        }
    }
    return;
}

# ── containers ───────────────────────────────────────────────────────
sub backup_container_prepare {
    my ($self, $vmid, $info) = @_;
    my $r = $self->{runs}->{$vmid} or die "backup_init was not called for $vmid\n";
    make_path($RUN_DIR);
    my $env = $self->env();
    $self->{'env-cache'} = $env;
    my $file = "$RUN_DIR/ct-$vmid.env";
    file_set_contents($file, join('', map { "$_=$env->{$_}\n" } sort keys %$env), 0400);
    chown $info->{'backup-user-id'}, -1, $file if defined $info->{'backup-user-id'};
    make_path($TMP_DIR);
    chmod 01777, $TMP_DIR;
    return;
}

sub backup_container {
    my ($self, $vmid, $guest_config, $exclude_patterns, $info) = @_;
    my $r = $self->{runs}->{$vmid} or die "backup_init was not called for $vmid\n";
    $r->{'guest-config'} = $guest_config;
    $r->{'firewall-config'} = $info->{'firewall-config'};
    my $req = {
        directory => $info->{directory}, sources => $info->{sources}, excludes => $exclude_patterns // [],
        guest_config => $guest_config, firewall_config => $info->{'firewall-config'},
    };
    my $res = $self->helper(
        ['tar', '--run', $r->{run}, '--dev', 'rootfs.tar', '--env-file', "$RUN_DIR/ct-$vmid.env",
         ($info->{'bandwidth-limit'} ? ('--bwlimit', $info->{'bandwidth-limit'}) : ())],
        { input => encode_json($req), 'log-progress' => 1 },
    );
    # we run forked here; carry the stats through the server instead of $self
    $self->log_info(sprintf("rootfs: %.0f MiB streamed, %d chunks new, %.0f MiB uploaded", ($res->{bytes_read} // 0) / 2**20,
        $res->{chunks_new} // 0, ($res->{bytes_uploaded} // 0) / 2**20));
    return;
}

# ── restore ──────────────────────────────────────────────────────────
sub run_of {
    my ($volname) = @_;
    die "unable to parse hvbackup archive '$volname'\n" if $volname !~ m!^backup/(vm|ct)/(\d+)/(\d+)$!;
    return ($1 eq 'vm' ? 'qemu' : 'lxc', $2, $3);
}

sub restore_get_mechanism {
    my ($self, $volname) = @_;
    my ($vmtype) = run_of($volname);
    return ('qemu-img', 'qemu') if $vmtype eq 'qemu';
    return ('tar', 'lxc');
}

sub archive_get_guest_config {
    my ($self, $volname) = @_;
    my (undef, undef, $run) = run_of($volname);
    my $res = $self->helper(['archive-config', '--run', $run], { timeout => 60 });
    return $res->{guest_config};
}

sub archive_get_firewall_config {
    my ($self, $volname) = @_;
    my (undef, undef, $run) = run_of($volname);
    my $res = $self->helper(['archive-config', '--run', $run], { timeout => 60 });
    return $res->{firewall_config};
}

sub restore_vm_init {
    my ($self, $volname) = @_;
    my (undef, undef, $run) = run_of($volname);
    my $res = $self->helper(['archive-volumes', '--run', $run], { timeout => 300 });
    my $out = {};
    for my $dev (keys %{ $res->{volumes} }) {
        $out->{$dev} = { size => $res->{volumes}->{$dev}->{size} };
    }
    $self->{restores}->{$volname} = { run => $run, servers => {} };
    return $out;
}

sub restore_vm_cleanup {
    my ($self, $volname) = @_;
    my $st = delete $self->{restores}->{$volname};
    return if !$st;
    for my $dev (keys %{ $st->{servers} }) {
        $self->stop_server($st->{servers}->{$dev});
    }
    return;
}

sub restore_vm_volume_init {
    my ($self, $volname, $device_name, $info) = @_;
    my (undef, undef, $run) = run_of($volname);
    my $st = $self->{restores}->{$volname} //= { run => $run, servers => {} };
    make_path($RUN_DIR);
    my $sock = "$RUN_DIR/restore-$run-$device_name-$$.sock";
    my $pidfile = "$sock.pid";
    unlink $sock, $pidfile;
    my $env = $self->env();
    my @cmd = map { PVE::Storage::Custom::HvbackupPlugin::untaint($_) } (@$HELPER, 'nbd-serve', '--run', $run, '--dev', $device_name, '--socket', $sock, '--pidfile', $pidfile);
    my $pid = fork() // die "fork failed: $!\n";
    if (!$pid) {
        local %ENV = (%ENV, map { $_ => PVE::Storage::Custom::HvbackupPlugin::untaint($env->{$_}) } keys %$env);
        $ENV{PATH} = '/usr/local/bin:/usr/bin:/bin';
        exec(@cmd) or POSIX::_exit(1);
    }
    for (1 .. 300) {
        last if -S $sock;
        usleep(100_000);
        if (waitpid($pid, WNOHANG) == $pid) {
            die "hvbackup NBD server for $device_name exited early\n";
        }
    }
    die "hvbackup NBD server for $device_name did not come up\n" if !-S $sock;
    my $srv = { pid => $pid, socket => $sock, pidfile => $pidfile };
    $st->{servers}->{$device_name} = $srv;

    # qemu-server stats the source before qemu-img reads it, so expose the export as a file
    # through nbdfuse (the same tool Proxmox uses on the backup side)
    my $mount = "$RUN_DIR/restore-$run-$device_name-$$.mnt";
    make_path($mount);
    my $file = "$mount/$device_name";
    my $fuse_pidfile = "$mount.pid";
    unlink $fuse_pidfile;
    my $fpid = fork() // die "fork failed: $!\n";
    if (!$fpid) {
        $ENV{PATH} = '/usr/local/bin:/usr/bin:/bin';
        exec('nbdfuse', '--readonly', '--pidfile', $fuse_pidfile, $file, "nbd+unix:///?socket=$sock") or POSIX::_exit(1);
    }
    $srv->{'fuse-pid'} = $fpid;
    $srv->{mount} = $mount;
    for (1 .. 300) {
        last if -f $file && -s $fuse_pidfile;
        usleep(100_000);
        if (waitpid($fpid, WNOHANG) == $fpid) {
            $self->stop_server($srv);
            die "nbdfuse for $device_name exited early\n";
        }
    }
    die "nbdfuse for $device_name did not come up\n" if !-f $file;
    $self->log_info("serving $device_name from the hvbackup repository over NBD as $file");
    return { 'qemu-img-path' => $file };
}

sub restore_vm_volume_cleanup {
    my ($self, $volname, $device_name, $info) = @_;
    my $st = $self->{restores}->{$volname} or return;
    my $srv = delete $st->{servers}->{$device_name} or return;
    $self->stop_server($srv);
    return;
}

sub stop_server {
    my ($self, $srv) = @_;
    if ($srv->{mount}) {
        eval { run_command(['fusermount3', '-u', $srv->{mount} . '/' . (split('/', $srv->{socket}))[-1]], errfunc => sub { }, outfunc => sub { }) };
        if ($srv->{'fuse-pid'}) {
            kill 'TERM', $srv->{'fuse-pid'};
            for (1 .. 50) {
                last if waitpid($srv->{'fuse-pid'}, WNOHANG) == $srv->{'fuse-pid'};
                usleep(100_000);
            }
            kill 'KILL', $srv->{'fuse-pid'};
            waitpid($srv->{'fuse-pid'}, 0);
        }
        unlink "$srv->{mount}.pid";
        rmdir $srv->{mount};
    }
    kill 'TERM', $srv->{pid};
    for (1 .. 50) {
        last if waitpid($srv->{pid}, WNOHANG) == $srv->{pid};
        usleep(100_000);
    }
    kill 'KILL', $srv->{pid};
    waitpid($srv->{pid}, 0);
    unlink $srv->{socket}, $srv->{pidfile};
    return;
}

sub restore_container_init {
    my ($self, $volname, $info) = @_;
    my (undef, undef, $run) = run_of($volname);
    make_path($TMP_DIR);
    my $tar = "$TMP_DIR/restore-$run-$$.tar";
    $self->log_info("fetching the container archive from the hvbackup repository to $tar");
    my $res = $self->helper(['fetch-tar', '--run', $run, '--dev', 'rootfs.tar', '--out', $tar], { 'log-progress' => 1 });
    $self->{restores}->{$volname} = { tar => $tar };
    return { 'tar-path' => $tar };
}

sub restore_container_cleanup {
    my ($self, $volname, $info) = @_;
    my $st = delete $self->{restores}->{$volname};
    unlink $st->{tar} if $st && $st->{tar};
    return;
}

1;
