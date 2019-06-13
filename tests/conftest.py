import shutil
import os
from contextlib import contextmanager
from pathlib import Path

from pytest import fixture, yield_fixture
from taf.repository_tool import Repository
from taf.utils import on_rm_error
import oll_sc
from tuf.repository_tool import (import_rsa_privatekey_from_file,
                                 import_rsa_publickey_from_file)

from .yubikey import (Root1YubiKey, Root2YubiKey, Root3YubiKey, TargetYubiKey,
                      init_pkcs11_mock)

TEST_DATA_PATH = Path(__file__).parent / 'data'
TEST_DATA_REPOS_PATH = TEST_DATA_PATH / 'repos'
TEST_DATA_ORIGIN_PATH = TEST_DATA_REPOS_PATH / 'origin'
KEYSTORE_PATH = TEST_DATA_PATH / 'keystore'
CLIENT_DIR_PATH = TEST_DATA_REPOS_PATH / 'client'


def pytest_configure(config):
  oll_sc.init_pkcs11 = init_pkcs11_mock


@contextmanager
def origin_repos(test_name):
  """Coppies git repository from `data/repos/test-XYZ` to data/repos/origin/XYZ
  path and renames `git` to `.git` for each repository.
  """

  temp_paths = {}
  test_dir_path = str(TEST_DATA_REPOS_PATH / test_name)
  for root, dirs, files in os.walk(test_dir_path):
   for dir_name in dirs:
     if dir_name == 'git':
      repo_rel_path = os.path.relpath(root, test_dir_path)
      dst_path = TEST_DATA_ORIGIN_PATH / test_name / repo_rel_path
      shutil.copytree(root, dst_path)
      (dst_path / 'git').rename(dst_path / '.git')
      repo_rel_path = Path(repo_rel_path).as_posix()
      temp_paths[repo_rel_path] = str(dst_path)

  yield temp_paths

  test_dst_path = str(TEST_DATA_ORIGIN_PATH / test_name)
  shutil.rmtree(test_dst_path, onerror=on_rm_error)


@yield_fixture(scope='session', autouse=True)
def taf_happy_path():
  """TAF repository for testing."""
  taf_repo_name = 'taf'
  test_dir = 'test-happy-path'

  with origin_repos(test_dir) as origins:
    taf_repo_origin_path = origins[taf_repo_name]
    taf_repo = Repository(taf_repo_origin_path)
    yield taf_repo

@yield_fixture(scope="session", autouse=True)
def updater_valid_test_repositories():
  test_dir = 'test-updater-valid'
  with origin_repos(test_dir) as origins:
    yield origins


@fixture
def client_dir():
  return CLIENT_DIR_PATH

@fixture
def keystore():
  """Keystore path."""
  return str(KEYSTORE_PATH)


@fixture
def targets_yk():
  """Targets YubiKey."""
  key = TargetYubiKey(KEYSTORE_PATH)
  yield key
  key.remove()


@fixture
def root1_yk():
  """Root1 YubiKey."""
  key = Root1YubiKey(KEYSTORE_PATH)
  yield key
  key.remove()


@fixture
def root2_yk():
  """Root2 YubiKey."""
  key = Root2YubiKey(KEYSTORE_PATH)
  yield key
  key.remove()


@fixture
def root3_yk():
  """Root3 YubiKey."""
  key = Root3YubiKey(KEYSTORE_PATH)
  yield key
  key.remove()


@fixture
def snapshot_key():
  """Snapshot key."""
  key = import_rsa_publickey_from_file(str(KEYSTORE_PATH / 'snapshot.pub'))
  priv_key = import_rsa_privatekey_from_file(str(KEYSTORE_PATH / 'snapshot'))
  key['keyval']['private'] = priv_key['keyval']['private']
  return key


@fixture
def timestamp_key():
  """Timestamp key."""
  key = import_rsa_publickey_from_file(str(KEYSTORE_PATH / 'timestamp.pub'))
  priv_key = import_rsa_privatekey_from_file(str(KEYSTORE_PATH / 'timestamp'))
  key['keyval']['private'] = priv_key['keyval']['private']
  return key