import click
import json
from taf.api.repository import (
    create_repository,
    add_dependency as add_dependency_to_dependencies_json,
    remove_dependency as remove_dependency_to_dependencies_json
)
from taf.updater.updater import update_repository, validate_repository, UpdateType


def attach_to_group(group):

    @group.group()
    def repo():
        pass

    @repo.command(context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
    ))
    @click.argument("auth_path")
    @click.argument("dependency_name")
    @click.argument("branch_name")
    @click.argument("out_of_band_commit")
    @click.option("--dependency-path", default=None, help="Dependency's filesystem path")
    @click.option("--keystore", default=None, help="Location of the keystore files")
    @click.pass_context
    def add_dependency(ctx, auth_path, dependency_name, branch_name, out_of_band_commit, dependency_path, keystore):
        """Add a dependency (an authentication repository) to dependencies.json or update it if it was already added to this file.
        Information that is added to dependencies.json includes out-of-band authentication commit and name
        of the branch which contains that commit. This out-of-band authentication commit represent a commit including and following
        which state of the authentication repository is valid at every commit. All additional information that should be saved as the
        dependency's custom content in `dependencies.json` is specified by providing additional options.

        `taf repo add-dependency auth-path namespace1/auth main d4d768da4e8f74f54c644923b7ed0e19a0faf3c5 --custom-property some-value

        In this case, custom-property: some-value will be added to the custom part of the dependency dependencies.json.

        Dependency does not have to exist on the filesystem, but if it does, provided branch name and out-of-band commit sha
        will be validated. If the dependency's full path is not provided, it is expected to be located in the same
        library root directory as the authentication repository, in a directory whose name corresponds to its name.
        If dependency's parent authentication repository's path is `E:\\examples\\root\\namespace\\auth`, and the dependency's namespace prefixed name is
        `namespace1\\auth`, the target's path will be set to `E:\\examples\\root\\namespace1\\auth`.

        """
        custom = {ctx.args[i][2:]: ctx.args[i + 1] for i in range(0, len(ctx.args), 2)} if len(ctx.args) else {}
        add_dependency_to_dependencies_json(
            auth_path=auth_path,
            dependency_name=dependency_name,
            branch_name=branch_name,
            out_of_band_commit=out_of_band_commit,
            dependency_path=dependency_path,
            keystore=keystore,
            custom=custom
        )

    @repo.command()
    @click.argument("path")
    @click.option("--keys-description", help="A dictionary containing information about the "
                  "keys or a path to a json file which stores the needed information")
    @click.option("--keystore", default=None, help="Location of the keystore files")
    @click.option("--commit", is_flag=True, default=False, help="Indicates if the changes should be "
                  "committed automatically")
    @click.option("--test", is_flag=True, default=False, help="Indicates if the created repository "
                  "is a test authentication repository")
    def create(path, keys_description, keystore, commit, test):
        """
        \b
        Create a new authentication repository at the specified location by registering
        signing keys and generating initial metadata files. Information about the roles
        can be provided through a dictionary - either specified directly or contained
        by a .json file whose path is specified when calling this command. This allows
        definition of:
            - total number of keys per role
            - threshold of signatures per role
            - should keys of a role be on Yubikeys or should keystore files be used
            - scheme (the default scheme is rsa-pkcs1v15-sha256)
            - keystore path, if not specified via keystore option

        \b
        For example:
        {
            "roles": {
                "root": {
                    "number": 3,
                    "length": 2048,
                    "passwords": ["password1", "password2", "password3"],
                    "threshold": 2,
                    "yubikey": true
                },
                "targets": {
                    "length": 2048
                },
                "snapshot": {},
                "timestamp": {}
            },
            "keystore": "keystore_path"
        }

        In cases when this dictionary is not specified, it is necessary to enter the needed
        information when asked to do so, or confirm that default values should be used.
        If keys should be stored in keystore files, it is possible to either use already generated
        keys (stored in keystore files located at the path specified using the keystore option),
        or to generate new one.

        If the test flag is set, a special target file will be created. This means that when
        calling the updater, it'll be necessary to use the --authenticate-test-repo flag.
        """
        create_repository(path, keystore, keys_description, commit, test)

    @repo.command()
    @click.argument("auth_path")
    @click.argument("dependency-name")
    @click.option("--keystore", default=None, help="Location of the keystore files")
    def remove_dependency(auth_path, dependency_name, keystore):
        """Remove a dependency from depdendencies.json"""
        remove_dependency_to_dependencies_json(auth_path, dependency_name, keystore)

    @repo.command()
    @click.argument("url")
    @click.argument("clients-auth-path", default=None, required=False)
    @click.option("--clients-library-dir", default=None, help="Directory where target repositories and, "
                  "optionally, authentication repository are located. If omitted it is "
                  "calculated based on authentication repository's path. "
                  "Authentication repo is presumed to be at root-dir/namespace/auth-repo-name")
    @click.option("--default-branch", default=None, help="Name of the default branch, like main or master")
    @click.option("--from-fs", is_flag=True, default=False, help="Indicates if the we want to clone a "
                  "repository from the filesystem")
    @click.option("--expected-repo-type", default="either", type=click.Choice(["test", "official", "either"]),
                  help="Indicates expected authentication repository type - test or official. If type is set to either, "
                  "the updater will not check the repository's type")
    @click.option("--scripts-root-dir", default=None, help="Scripts root directory, which can be used to move scripts "
                  "out of the authentication repository for testing purposes (avoid dirty index). Scripts will be expected "
                  "to be located in scripts_root_dir/repo_name directory")
    @click.option("--profile", is_flag=True, help="Flag used to run profiler and generate .prof file")
    @click.option("--format-output", is_flag=True, help="Return formatted output which includes information "
                  "on if build was successful and error message if it was raised")
    @click.option("--exclude-target", multiple=True, help="globs defining which target repositories should be "
                  "ignored during update.")
    @click.option("--strict", is_flag=True, default=False, help="Enable/disable strict mode - return an error"
                  "if warnings are raised ")
    def update(url, clients_auth_path, clients_library_dir, default_branch, from_fs, expected_repo_type,
               scripts_root_dir, profile, format_output, exclude_target, strict):
        """
        Update and validate local authentication repository and target repositories. Remote
        authentication's repository url needs to be specified when calling this command. If the
        authentication repository and the target repositories are in the same root directory,
        locations of the target repositories are calculated based on the authentication repository's
        path. If that is not the case, it is necessary to redefine this default value using the
        --clients-library-dir option. This means that if authentication repository's path is
        E:\\root\\namespace\\auth-repo, it will be assumed that E:\\root is the root directory
        if clients-library-dir is not specified.
        Names of target repositories (as defined in repositories.json) are appended to the root repository's
        path thus defining the location of each target repository. If names of target repositories
        are namespace/repo1, namespace/repo2 etc and the root directory is E:\\root, path of the target
        repositories will be calculated as E:\\root\\namespace\\repo1, E:\\root\\namespace\\root2 etc.

        Path to authentication repository directory is set by clients-auth-path argument. If this
        argument is not provided, it is expected that --clients-library-dir option is used. The updater
        will raise an error if one of both isn't set.

        If remote repository's url is a file system path, it is necessary to call this command with
        --from-fs flag so that url validation is skipped. When updating a test repository (one that has
        the "test" target file), use --authenticate-test-repo flag. An error will be raised
        if this flag is omitted in the mentioned case. Do not use this flag when validating a non-test
        repository as that will also result in an error.

        Scripts root directory option can be used to move scripts out of the authentication repository for
        testing purposes (avoid dirty index). Scripts will be expected  o be located in scripts_root_dir/repo_name directory

        One or more target repositories can be excluded from the update process using --exclude-target.
        In that case, the library will only be partly validated, so last_validate_commit will not be updated
        and no scripts will be called.

        Update can be in strict or no-strict mode. Strict mode is set by specifying --strict, which will raise errors
        during update if any/all warnings are found. By default, --strict is disabled.
        """
        if clients_auth_path is None and clients_library_dir is None:
            raise click.UsageError('Must specify either authentication repository path or library directory!')

        if profile:
            import cProfile
            import atexit

            print("Profiling...")
            pr = cProfile.Profile()
            pr.enable()

            def exit():
                pr.disable()
                print("Profiling completed")
                filename = 'updater.prof'  # You can change this if needed
                pr.dump_stats(filename)

            atexit.register(exit)

        try:
            update_repository(
                url,
                clients_auth_path,
                clients_library_dir,
                default_branch,
                from_fs,
                UpdateType(expected_repo_type),
                scripts_root_dir=scripts_root_dir,
                excluded_target_globs=exclude_target,
                strict=strict
            )
            if format_output:
                print(json.dumps({
                    'updateSuccessful': True
                }))
        except Exception as e:
            if format_output:
                error_data = {
                    'updateSuccessful': False,
                    'error': str(e)
                }
                print(json.dumps(error_data))
            else:
                raise e

    @repo.command()
    @click.argument("clients-auth-path")
    @click.option("--clients-library-dir", default=None, help="Directory where target repositories and, "
                  "optionally, authentication repository are located. If omitted it is "
                  "calculated based on authentication repository's path. "
                  "Authentication repo is presumed to be at library-dir/namespace/auth-repo-name")
    @click.option("--default-branch", default=None, help="Name of the default branch, like mian or master")
    @click.option("--from-commit", default=None, help="First commit which should be validated.")
    @click.option("--exclude-target", multiple=True, help="globs defining which target repositories should be "
                  "ignored during update.")
    @click.option("--strict", is_flag=True, default=False, help="Enable/disable strict mode - return an error"
                  "if warnings are raised")
    def validate(clients_auth_path, clients_library_dir, default_branch, from_commit, exclude_target, strict):
        """
        Validates an authentication repository which is already on the file system
        and its target repositories (which are also expected to be on the file system).
        Does not clone repositories, fetch changes or merge commits.

        Validation can be in strict or no-strict mode. Strict mode is set by specifying --strict, which will raise errors
        during validate if any/all warnings are found. By default, --strict is disabled.
        """
        validate_repository(clients_auth_path, clients_library_dir, default_branch, from_commit, exclude_target, strict)
