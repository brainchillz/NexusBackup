"""Every test module runs against throwaway data and repository directories. Set before any
hvbackup module is imported, because hvbackup.config reads the environment at import."""
import os
import tempfile

os.environ.setdefault('HVB_DATA_DIR', tempfile.mkdtemp(prefix='hvb-test-data-'))
os.environ.setdefault('HVB_REPO', tempfile.mkdtemp(prefix='hvb-test-repo-'))
