import datetime
import json
import os
from binascii import hexlify
from collections import defaultdict
from functools import partial
from getpass import getpass
from pathlib import Path

import click
import securesystemslib
import securesystemslib.exceptions
import tuf.repository_tool
from taf.auth_repo import AuthenticationRepo
from taf.constants import DEFAULT_RSA_SIGNATURE_SCHEME
from taf.exceptions import KeystoreError
from taf.git import GitRepository
from taf.keystore import (
    key_cmd_prompt,
    read_private_key_from_keystore,
    read_public_key_from_keystore,
    new_public_key_cmd_prompt,
)
from taf.log import taf_logger
from taf.repository_tool import Repository
from taf.utils import read_input_dict
from tuf.repository_tool import (
    METADATA_DIRECTORY_NAME,
    TARGETS_DIRECTORY_NAME,
    create_new_repository,
    generate_and_write_rsa_keypair,
    generate_rsa_key,
)

try:
    import taf.yubikey as yk
except ImportError:
    taf_logger.warning('"yubikey-manager" is not installed.')

# Yubikey x509 certificate expiration interval
EXPIRATION_INTERVAL = 36500
YUBIKEY_EXPIRATION_DATE = datetime.datetime.now() + datetime.timedelta(
    days=EXPIRATION_INTERVAL
)


def add_signing_key(
    repo_path, role, pub_key_path=None, scheme=DEFAULT_RSA_SIGNATURE_SCHEME
):
    """
    Adds a new signing key. Currently assumes that all relevant keys are stored on yubikeys.
    Allows us to add a new targets key for example
    """
    from taf.repository_tool import yubikey_signature_provider

    taf_repo = Repository(repo_path)
    pub_key_pem = None
    if pub_key_path is not None:
        pub_key_path = Path(pub_key_path)
        if pub_key_path.is_file():
            pub_key_pem = Path(pub_key_path).read_text()

    if pub_key_pem is None:
        pub_key_pem = new_public_key_cmd_prompt(scheme)["keyval"]["public"]

    taf_repo.add_metadata_key(role, pub_key_pem, scheme)
    root_obj = taf_repo._repository.root
    threshold = root_obj.threshold
    keys_num = len(root_obj.keys)
    num_of_signatures = 0
    loaded_yubikeys = {}
    pub_key, _ = yk.yubikey_prompt(
        role, role, taf_repo, loaded_yubikeys=loaded_yubikeys
    )
    role_obj = _role_obj(role, taf_repo._repository)
    role_obj.add_external_signature_provider(
        pub_key, partial(yubikey_signature_provider, role, pub_key["keyid"])
    )

    all_loaded = False
    while not all_loaded:
        if num_of_signatures >= threshold:
            all_loaded = not (
                click.confirm(
                    "Threshold of root keys reached. Do you want to load more root keys?"
                )
            )
        if not all_loaded:
            name = f"root{num_of_signatures+1}"
            pub_key, _ = yk.yubikey_prompt(
                name, "root", taf_repo, loaded_yubikeys=loaded_yubikeys
            )
            root_obj.add_external_signature_provider(
                pub_key, partial(yubikey_signature_provider, name, pub_key["keyid"])
            )
            num_of_signatures += 1
        if num_of_signatures == keys_num:
            all_loaded = True
    taf_repo.writeall()


def _load_signing_keys(
    taf_repo,
    role,
    keystore=None,
    role_key_infos=None,
    loaded_yubikeys=None,
    scheme=DEFAULT_RSA_SIGNATURE_SCHEME,
):
    """
    Load role's signing keys. Make sure that at least the threshold of keys was
    loaded, but allow loading more keys (so that a metadata file can be signed
    by all of the role's keys if the user wants that)
    """
    print(f"Loading signing keys of role {role}")
    threshold = taf_repo.get_role_threshold(role)
    signing_keys_num = len(taf_repo.get_role_keys(role))
    all_loaded = False
    num_of_signatures = 0
    keys = []
    if keystore is not None:
        # try loading files from the keystore first
        # does not assume that all keys of a role are stored in keystore files just
        # because one of them is
        keystore = Path(keystore)
        # names of keys are expected to be role or role + counter
        key_names = [f"{role}{counter}" for counter in range(1, signing_keys_num + 1)]
        key_names.insert(0, role)
        for key_name in key_names:
            if (keystore / key_name).is_file():
                key = read_private_key_from_keystore(
                    keystore, key_name, role_key_infos, num_of_signatures, scheme
                )
                keys.append(key)
                num_of_signatures += 1

    while not all_loaded and num_of_signatures < signing_keys_num:
        if signing_keys_num == 1:
            key_name = role
        else:
            key_name = f"{role}{num_of_signatures + 1}"
        if num_of_signatures >= threshold:
            all_loaded = not (
                click.confirm(
                    f"Threshold of {role} keys reached. Do you want to load more {role} keys?"
                )
            )
        if not all_loaded:
            is_yubikey = None
            if role_key_infos is not None and role in role_key_infos:
                is_yubikey = role_key_infos[role].get("yubikey")
            if is_yubikey is None:
                is_yubikey = click.confirm(f"Sign {role} using YubiKey(s)?")
            if is_yubikey:
                yk.yubikey_prompt(
                    key_name, role, taf_repo, loaded_yubikeys=loaded_yubikeys
                )
            else:
                key = key_cmd_prompt(key_name, role, taf_repo, keys, scheme)
                keys.append(key)
        num_of_signatures += 1
    return keys


def _update_target_repos(repo_path, targets_dir, target_repo_path, add_branch):
    """Updates target repo's commit sha and branch"""
    if not target_repo_path.is_dir() or target_repo_path == repo_path:
        return
    target_repo = GitRepository(str(target_repo_path))
    if target_repo.is_git_repository:
        data = {"commit": target_repo.head_commit_sha()}
        if add_branch:
            data["branch"] = target_repo.get_current_branch()
        target_repo_name = target_repo_path.name
        path = targets_dir / target_repo_name
        path.write_text(json.dumps(data, indent=4))
        print(f"Updated {path}")


def update_target_repos_from_fs(
    repo_path, root_dir=None, namespace=None, add_branch=True
):
    """
    <Purpose>
        Create or update target files by reading the latest commits of the provided target repositories
    <Arguments>
        repo_path:
        Authentication repository's location
        root_dir:
        Directory where target repositories and, optionally, authentication repository are locate
        namespace:
        Namespace used to form the full name of the target repositories. Each target repository
        add_branch:
        Indicates whether to add the current branch's name to the target file
    """
    repo_path = Path(repo_path).resolve()
    namespace, root_dir = _get_namespace_and_root(repo_path, namespace, root_dir)
    targets_directory = root_dir / namespace
    print(
        f"Updating target files corresponding to repos located at {targets_directory}"
    )
    auth_repo_targets_dir = repo_path / TARGETS_DIRECTORY_NAME
    if namespace:
        auth_repo_targets_dir = auth_repo_targets_dir / namespace
        auth_repo_targets_dir.mkdir(parents=True, exist_ok=True)
    for target_repo_path in targets_directory.glob("*"):
        _update_target_repos(
            repo_path, auth_repo_targets_dir, target_repo_path, add_branch
        )


def update_target_repos_from_repositories_json(
    repo_path, root_dir, namespace, add_branch=True
):
    """
    <Purpose>
        Create or update target files by reading the latest commit's repositories.json
    <Arguments>
        repo_path:
        Authentication repository's location
        root_dir:
        Directory where target repositories and, optionally, authentication repository are locate
        namespace:
        Namespace used to form the full name of the target repositories. Each target repository
        add_branch:
        Indicates whether to add the current branch's name to the target file
    """
    repo_path = Path(repo_path).resolve()
    auth_repo_targets_dir = repo_path / TARGETS_DIRECTORY_NAME
    repositories_json = json.loads(
        Path(auth_repo_targets_dir / "repositories.json").read_text()
    )
    namespace, root_dir = _get_namespace_and_root(repo_path, namespace, root_dir)
    print(
        f"Updating target files corresponding to repos located at {(root_dir / namespace)}"
        "and specified in repositories.json"
    )
    for repo_name in repositories_json.get("repositories"):
        target_repo_path = root_dir / repo_name
        namespace_and_name = repo_name.rsplit("/", 1)
        if len(namespace_and_name) > 1:
            namespace, _ = namespace_and_name
            targets_dir = auth_repo_targets_dir / namespace
        targets_dir.mkdir(parents=True, exist_ok=True)
        _update_target_repos(repo_path, targets_dir, target_repo_path, add_branch)


def _register_yubikey(yubikeys, role_obj, role_name, key_name, scheme, certs_dir):
    from taf.repository_tool import yubikey_signature_provider

    registered = False
    while not registered:
        print(f"Registering keys for {key_name}")
        use_existing = click.confirm("Do you want to reuse already set up Yubikey?")

        if not use_existing:
            if not click.confirm(
                "WARNING - this will delete everything from the inserted key. Proceed?"
            ):
                continue

        key, serial_num = yk.yubikey_prompt(
            key_name,
            role_name,
            taf_repo=None,
            registering_new_key=True,
            creating_new_key=not use_existing,
            loaded_yubikeys=yubikeys,
            pin_confirm=True,
            pin_repeat=True,
        )

        if not use_existing:
            key = yk.setup_new_yubikey(serial_num, scheme)
        export_yk_certificate(certs_dir, key)

        registered = True
        # set Yubikey expiration date
        # add_yubikey_external_signature_provider
        role_obj.add_verification_key(key, expires=YUBIKEY_EXPIRATION_DATE)
        role_obj.add_external_signature_provider(
            key, partial(yubikey_signature_provider, key_name, key["keyid"])
        )


def _register_key(
    keystore, roles_key_info, role_obj, role_name, key_name, key_num, scheme, length
):
    # if keystore exists, load the keys
    generate_new_keys = keystore is None
    if keystore is not None:
        try:
            public_key = read_public_key_from_keystore(keystore, key_name, scheme)
            private_key = read_private_key_from_keystore(
                keystore, key_name, roles_key_info, key_num, scheme
            )
        except KeystoreError as e:
            generate_new_keys = click.confirm(
                f"Could not load {key_name}. Generate new keys?"
            )
            if not generate_new_keys:
                raise e
    if generate_new_keys:
        if keystore is not None and click.confirm("Write keys to keystore files?"):
            generate_and_write_rsa_keypair(
                str(Path(keystore) / key_name), bits=length, password=""
            )
            public_key = read_public_key_from_keystore(keystore, key_name, scheme)
            private_key = read_private_key_from_keystore(
                keystore, key_name, roles_key_info, key_num, scheme
            )
        else:
            key = generate_rsa_key(bits=length, scheme=scheme)
            print(f"{role_name} key:\n\n{key['keyval']['private']}\n\n")
            public_key = private_key = key
    role_obj.add_verification_key(public_key)
    role_obj.load_signing_key(private_key)


def create_repository(
    repo_path, keystore=None, roles_key_infos=None, commit=False, test=False
):
    """
    <Purpose>
        Create a new authentication repository. Generate initial metadata files.
        The initial targets metadata file is empty (does not specify any targets).
    <Arguments>
        repo_path:
        Authentication repository's location
        targets_directory:
        Directory which contains target repositories
        keystore:
        Location of the keystore files
        roles_key_infos:
        A dictionary whose keys are role names, while values contain information about the keys.
        commit:
        Indicates if the changes should be automatically comitted
        test:
        Indicates if the created repository is a test authentication repository
    """
    yubikeys = defaultdict(dict)
    roles_key_infos = read_input_dict(roles_key_infos)
    if not len(roles_key_infos):
        # ask the user to enter roles, number of keys etc.
        roles_key_infos = _enter_roles_infos()

    repo = AuthenticationRepo(repo_path)
    if Path(repo_path).is_dir():
        if repo.is_git_repository:
            print(f"Repository {repo_path} already exists")
            return

    tuf.repository_tool.METADATA_STAGED_DIRECTORY_NAME = METADATA_DIRECTORY_NAME
    repository = create_new_repository(repo_path)

    def _register_roles_keys(role_name, key_info, repository):
        num_of_keys = key_info.get("number", 1)
        threshold = key_info.get("threshold", 1)
        is_yubikey = key_info.get("yubikey", False)
        scheme = key_info.get("scheme", DEFAULT_RSA_SIGNATURE_SCHEME)
        length = key_info.get("length", 3072)
        role_obj = _role_obj(role_name, repository)
        role_obj.threshold = threshold
        for key_num in range(num_of_keys):
            key_name = _get_key_name(role_name, key_num, num_of_keys)
            if is_yubikey:
                _register_yubikey(
                    yubikeys, role_obj, role_name, key_name, scheme, repo.certs_dir
                )
            else:
                _register_key(
                    keystore,
                    key_info,
                    role_obj,
                    role_name,
                    key_name,
                    key_num,
                    scheme,
                    length,
                )

    try:
        # load keys not stored on YubiKeys first, to avoid entering pins
        # if there is somethig wrong with keystore files
        for role_name, key_info in roles_key_infos.items():
            if not key_info.get("yubikey", False):
                _register_roles_keys(role_name, key_info, repository)

        for role_name, key_info in roles_key_infos.items():
            if key_info.get("yubikey", False):
                _register_roles_keys(role_name, key_info, repository)

    except KeystoreError as e:
        print(f"Creation of repository failed: {e}")
        return

    # if the repository is a test repository, add a target file called test-auth-repo
    if test:
        target_paths = Path(repo_path) / "targets"
        test_auth_file = target_paths / "test-auth-repo"
        test_auth_file.touch()
        targets_obj = _role_obj("targets", repository)
        targets_obj.add_target(str(test_auth_file))

    repository.writeall()
    print("Created new authentication repository")
    if commit:
        auth_repo = GitRepository(repo_path)
        auth_repo.init_repo()
        commit_message = input("\nEnter commit message and press ENTER\n\n")
        auth_repo.commit(commit_message)


def _enter_roles_infos():
    mandatory_roles = ["root", "targets", "snapshot", "timestamp"]
    role_key_infos = defaultdict(dict)

    def _read_val(input_type, name):
        while True:
            try:
                val = input(
                    f"Enter {name} and press ENTER. Leave empty to use the default value. "
                )
                if not val:
                    return val
                return int(val)
            except ValueError:
                pass

    def _enter_role_info(role):
        role_key_infos[role]["number"] = _read_val(int, f"number of {role} keys")
        role_key_infos[role]["length"] = _read_val(int, f"{role} key length")
        role_key_infos[role]["threshold"] = _read_val(
            int, f"{role} signature threshold"
        )
        role_key_infos[role]["yubikey"] = click.confirm(
            f"Store {role} keys on Yubikeys?"
        )
        role_key_infos[role]["scheme"] = _read_val(str, f"{role} signature scheme")

    for role in mandatory_roles:
        _enter_role_info(role)

    # delegated targets role
    while click.confirm("Add a delegated targets role?"):
        role_name = input("Enter role name and press ENTER")
        if role_name:
            _enter_role_info(role_name)

    return role_key_infos


def export_yk_public_pem(path=None):
    try:
        pub_key_pem = yk.export_piv_pub_key().decode("utf-8")
    except Exception:
        print("Could not export the public key. Check if a YubiKey is inserted")
        return
    if path is None:
        print(pub_key_pem)
    else:
        if not path.endswith(".pub"):
            path = f"{path}.pub"
        path = Path(path)
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pub_key_pem)


def export_yk_certificate(certs_dir, key):
    if certs_dir is None:
        certs_dir = Path.home()
    else:
        certs_dir = Path(certs_dir)
    certs_dir.mkdir(parents=True, exist_ok=True)
    cert_path = certs_dir / f"{key['keyid']}.cert"
    print(f"Exporting certificate to {cert_path}")
    with open(cert_path, "wb") as f:
        f.write(yk.export_piv_x509())


def _get_namespace_and_root(repo_path, namespace, root_dir):
    if namespace is None:
        namespace = repo_path.parent.name
    if root_dir is None:
        root_dir = repo_path.parent.parent
    else:
        root_dir = Path(root_dir).resolve()
    return namespace, root_dir


def generate_keys(keystore, roles_key_infos):
    """
    <Purpose>
        Generate public and private keys and writes them to disk. Names of keys correspond to names
        of the TUF roles. If more than one key should be generated per role, a counter is appended
        to the role's name. E.g. root1, root2, root3 etc.
    <Arguments>
        keystore:
        Location where the generated files should be saved
        roles_key_infos:
        A dictionary whose keys are role names, while values contain information about the keys.
        This includes:
            - passwords of the keystore files
            - number of keys per role (optional, defaults to one if not provided)
            - key length (optional, defaults to TUF's default value, which is 3072)
        Names of the keys are set to names of the roles plus a counter, if more than one key
        should be generated.
    """
    roles_key_infos = read_input_dict(roles_key_infos)
    for role_name, key_info in roles_key_infos.items():
        num_of_keys = key_info.get("number", 1)
        bits = key_info.get("length", 3072)
        passwords = key_info.get("passwords", [""] * num_of_keys)
        is_yubikey = key_info.get("yubikey", False)
        for key_num in range(num_of_keys):
            if not is_yubikey:
                key_name = _get_key_name(role_name, key_num, num_of_keys)
                password = passwords[key_num]
                path = str(Path(keystore, key_name))
                print(f"Generating {path}")
                generate_and_write_rsa_keypair(path, bits=bits, password=password)


def generate_repositories_json(
    repo_path,
    root_dir=None,
    namespace=None,
    targets_relative_dir=None,
    custom_data=None,
):
    """
    <Purpose>
        Generatesinitial repositories.json
    <Arguments>
        repo_path:
        Authentication repository's location
        root_dir:
        Directory where target repositories and, optionally, authentication repository are locate
        namespace:
        Namespace used to form the full name of the target repositories. Each target repository
        is expected to be root_dir/namespace directory
        targets_relative_dir:
        Directory relative to which urls of the target repositories are set, if they do not have remote set
        custom_date:
        Dictionary or path to a json file containing additional information about the repositories that
        should be added to repositories.json
    """

    custom_data = read_input_dict(custom_data)
    repositories = {}
    repo_path = Path(repo_path).resolve()
    auth_repo_targets_dir = repo_path / TARGETS_DIRECTORY_NAME
    # if targets directory is not specified, assume that target repositories
    # and the authentication repository are in the same parent direcotry
    namespace, root_dir = _get_namespace_and_root(
        repo_path, namespace, root_dir
    )
    targets_directory = root_dir / namespace
    if targets_relative_dir is not None:
        targets_relative_dir = Path(targets_relative_dir).resolve()

    print(f"Adding all repositories from {targets_directory}")
    for target_repo_dir in targets_directory.glob("*"):
        if not target_repo_dir.is_dir() or target_repo_dir == repo_path:
            continue
        target_repo = GitRepository(target_repo_dir.resolve())
        if not target_repo.is_git_repository:
            continue
        target_repo_name = target_repo_dir.name
        target_repo_namespaced_name = (
            target_repo_name if not namespace else f"{namespace}/{target_repo_name}"
        )
        # determine url to specify in initial repositories.json
        # if the repository has a remote set, use that url
        # otherwise, set url to the repository's absolute or relative path (relative
        # to targets_relative_dir if it is specified)
        url = target_repo.get_remote_url()
        if url is None:
            if targets_relative_dir is not None:
                url = Path(os.path.relpath(target_repo.repo_path, targets_relative_dir))
            else:
                url = Path(target_repo.repo_path).resolve()
            # convert to posix path
            url = str(url.as_posix())
        repositories[target_repo_namespaced_name] = {"urls": [url]}
        if target_repo_namespaced_name in custom_data:
            repositories[target_repo_namespaced_name]["custom"] = custom_data[
                target_repo_namespaced_name
            ]

    file_path = auth_repo_targets_dir / "repositories.json"
    file_path.write_text(json.dumps({"repositories": repositories}, indent=4))
    print(f"Generated {file_path}")


def _get_key_name(role_name, key_num, num_of_keys):
    if num_of_keys == 1:
        return role_name
    else:
        return role_name + str(key_num + 1)


def init_repo(
    repo_path,
    root_dir=None,
    namespace=None,
    targets_relative_dir=None,
    custom_data=None,
    add_branch=None,
    keystore=None,
    roles_key_infos=None,
    commit=False,
    test=False,
    scheme=DEFAULT_RSA_SIGNATURE_SCHEME,
):
    """
    <Purpose>
        Generate initial repository:
        1. Crete tuf authentication repository
        2. Commit initial metadata files if commit == True
        3. Add target repositories
        4. Generate repositories.json
        5. Update tuf metadata
        6. Commit the changes if commit == True
    <Arguments>
        repo_path:
        Authentication repository's location
        root_dir:
        Directory where target repositories and, optionally, authentication repository are locate
        namespace:
        Namespace used to form the full name of the target repositories. Each target repository
        is expected to be root_dir/namespace directory
        targets_relative_dir:
        Directory relative to which urls of the target repositories are set, if they do not have remote set
        custom_date:
        Dictionary or path to a json file containing additional information about the repositories that
        should be added to repositories.json
        add_branch:
        Indicates whether to add the current branch's name to the target file
        keystore:
        Location of the keystore files
        roles_key_infos:
        A dictionary whose keys are role names, while values contain information about the keys.
        test:
        Indicates if the created repository is a test authentication repository
        scheme:
        A signature scheme used for signing.
    """
    # read the key infos here, no need to read the file multiple times
    namespace, root_dir = _get_namespace_and_root(
        repo_path, namespace, root_dir
    )
    targets_directory = root_dir / namespace
    roles_key_infos = read_input_dict(roles_key_infos)
    create_repository(repo_path, keystore, roles_key_infos, commit, test)
    update_target_repos_from_fs(repo_path, targets_directory, namespace, add_branch)
    generate_repositories_json(
        repo_path, root_dir, namespace, targets_relative_dir, custom_data
    )
    register_target_files(repo_path, keystore, roles_key_infos, commit, scheme=scheme)


def register_target_files(
    repo_path,
    keystore=None,
    roles_key_infos=None,
    commit=False,
    scheme=DEFAULT_RSA_SIGNATURE_SCHEME,
):
    """
    <Purpose>
        Register all files found in the target directory as targets - updates the targets
        metadata file, snapshot and timestamp. Sign targets
        with yubikey if keystore is not provided
    <Arguments>
        repo_path:
        Authentication repository's path
        keystore:
        Location of the keystore files
        roles_key_infos:
        A dictionary whose keys are role names, while values contain information about the keys.
        commit_msg:
        Commit message. If specified, the changes made to the authentication are committed.
        scheme:
        A signature scheme used for signing.
    """
    print("Signing target files")
    roles_key_infos = read_input_dict(roles_key_infos)
    repo_path = Path(repo_path).resolve()
    targets_path = repo_path / TARGETS_DIRECTORY_NAME
    taf_repo = Repository(str(repo_path))
    for root, _, filenames in os.walk(str(targets_path)):
        for filename in filenames:
            taf_repo.add_existing_target(str(Path(root) / filename))
    _write_targets_metadata(taf_repo, keystore, roles_key_infos, scheme)
    if commit:
        auth_repo = GitRepository(repo_path)
        commit_message = input("\nEnter commit message and press ENTER\n\n")
        auth_repo.commit(commit_message)


def _role_obj(role, repository):
    if role == "targets":
        return repository.targets
    elif role == "snapshot":
        return repository.snapshot
    elif role == "timestamp":
        return repository.timestamp
    elif role == "root":
        return repository.root


def signature_provider(key_id, cert_cn, key, data):  # pylint: disable=W0613
    def _check_key_id(expected_key_id):
        try:
            inserted_key = yk.get_piv_public_key_tuf()
            return expected_key_id == inserted_key["keyid"]
        except Exception:
            return False

    while not _check_key_id(key_id):
        pass

    data = securesystemslib.formats.encode_canonical(data).encode("utf-8")
    key_pin = getpass(f"Please insert {cert_cn} YubiKey, input PIN and press ENTER.\n")
    signature = yk.sign_piv_rsa_pkcs1v15(data, key_pin)

    return {"keyid": key_id, "sig": hexlify(signature).decode()}


def setup_signing_yubikey(certs_dir=None, scheme=DEFAULT_RSA_SIGNATURE_SCHEME):
    if not click.confirm(
        "WARNING - this will delete everything from the inserted key. Proceed?"
    ):
        return
    _, serial_num = yk.yubikey_prompt(
        "new Yubikey",
        creating_new_key=True,
        pin_confirm=True,
        pin_repeat=True,
        prompt_message="Please insert the new Yubikey and press ENTER",
    )
    key = yk.setup_new_yubikey(serial_num)
    export_yk_certificate(certs_dir, key)


def setup_test_yubikey(key_path=None):
    """
    Resets the inserted yubikey, sets default pin and copies the specified key
    onto it.
    """
    if not click.confirm("WARNING - this will reset the inserted key. Proceed?"):
        return
    key_path = Path(key_path)
    key_pem = key_path.read_bytes()

    print(f"Importing RSA private key from {key_path} to Yubikey...")
    pin = yk.DEFAULT_PIN

    pub_key = yk.setup(pin, "Test Yubikey", private_key_pem=key_pem)
    print("\nPrivate key successfully imported.\n")
    print("\nPublic key (PEM): \n{}".format(pub_key.decode("utf-8")))
    print("Pin: {}\n".format(pin))


def update_metadata_expiration_date(
    repo_path,
    role,
    interval,
    keystore=None,
    scheme=None,
    start_date=datetime.datetime.now(),
    commit=False,
):
    taf_repo = Repository(repo_path)
    update_methods = {
        "timestamp_keystore": taf_repo.update_timestamp,
        "snapshot_keystore": taf_repo.update_snapshot,
        "targets_keystore": taf_repo.update_targets_from_keystore,
        "targets_yubikey": taf_repo.update_targets,
    }
    loaded_yubikeys = {}
    keys = _load_signing_keys(
        taf_repo,
        role,
        loaded_yubikeys=loaded_yubikeys,
        keystore=keystore,
        scheme=scheme,
    )
    if len(keys):
        try:
            update_methods[f"{role}_keystore"](keys[0], start_date, interval)
        except KeyError:
            print(f"Cannot update {role} from keystore")
    else:
        for serial_num in loaded_yubikeys:
            if "targets" in loaded_yubikeys[serial_num]:
                pin = yk.get_key_pin(serial_num)
                try:
                    update_methods[f"{role}_yubikey"](pin, None, start_date, interval)
                except KeyError:
                    print(f"Cannot update {role} using yubikeys")
                break
    print(f"Updated expiration date of {role}")

    if commit:
        auth_repo = GitRepository(repo_path)
        commit_message = input("\nEnter commit message and press ENTER\n\n")
        auth_repo.commit(commit_message)


def _write_targets_metadata(taf_repo, keystore, roles_key_infos, scheme):

    loaded_yubikeys = {}
    targets_keys = _load_signing_keys(
        taf_repo, "targets", keystore, roles_key_infos, loaded_yubikeys, scheme=scheme
    )
    if len(targets_keys):
        taf_repo.update_targets_from_keystore(targets_keys[0], write=False)
    else:
        for serial_num in loaded_yubikeys:
            if "targets" in loaded_yubikeys[serial_num]:
                pin = yk.get_key_pin(serial_num)
                taf_repo.update_targets(pin, write=False)
                break
    snapshot_keys = _load_signing_keys(
        taf_repo, "snapshot", keystore, roles_key_infos, scheme=scheme
    )
    timestamp_keys = _load_signing_keys(
        taf_repo, "timestamp", keystore, roles_key_infos, scheme=scheme
    )
    snapshot_key = snapshot_keys[0]
    timestamp_key = timestamp_keys[0]
    taf_repo.update_snapshot_and_timestmap(snapshot_key, timestamp_key, write=False)
    taf_repo.writeall()


# TODO Implement update of repositories.json (updating urls, custom data, adding new repository, removing
# repository etc.)
# TODO create tests for this
