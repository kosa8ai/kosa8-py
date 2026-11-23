# Compat results: other SDKs' own tests and examples on the kosa8 shims

Run 2026-11-23 against https://api.kosa8.com by `compat_suite/run.py`. Each competitor's own test
suite or examples run **unmodified**; only their SDK's names are pointed at the kosa8 shim
(`kosa8.compat.e2b`, `kosa8.compat.daytona`, `kosa8.compat.modal`). A case is *pass* (ran to the end),
*unsupported* (stopped at a call the shim refuses with `Unsupported`, which names the kosa8
alternative), *fail* (anything else) or *skip* (skipped by the suite itself). Pass rate =
pass / (pass + unsupported + fail). Rerun it yourself: `python compat_suite/run.py`.

| Suite | Source | Pass | Unsupported | Fail | Skip | Pass rate |
|---|---|---|---|---|---|---|
| E2B Python SDK 2.64.0 | `e2b-dev/E2B@08c4eeb` tests/sync/sandbox_sync | 89 | 47 | 43 | 0 | 89/179 (50%) |
| Daytona Python SDK 2.1.0 | `daytonaio/daytona@b5a5d9e` examples/python | 9 | 7 | 1 | 0 | 9/17 (53%) |
| Modal Python SDK 1.6.4 | `modal-labs/modal-examples@50b16f6` 13_sandboxes | 0 | 7 | 4 | 0 | 0/11 (0%) |

## E2B Python SDK 2.64.0

| Case | Outcome | Why |
|---|---|---|
| `commands.test_env_vars::test_bash_command_scoped_env_vars` | fail | E       kosa8.compat.e2b.CommandExitException: Command exited with code 127 and error: |
| `commands.test_run::test_run_with_broken_utf8` | fail | E           kosa8.compat.e2b.InvalidArgumentException: cmd must be a non-empty list of strings, e.g. ["echo", "hi"] [HTTP 400 invalid_cmd; request a5f14ed28e6c83c3-BOS] |
| `commands.test_run::test_run_with_too_short_timeout_iterating` | fail | tests/sync/sandbox_sync/commands/test_run.py:56: Failed |
| `commands.test_sandbox_killed_during_run::test_kill_sandbox_while_command_is_running` | fail | E           kosa8.compat.e2b.NotFoundException: no such sandbox [HTTP 404 not_found; request a5f14f83eb888f6f-BOS] |
| `files.test_files_list::test_directory_entry_details` | fail | E       AssertionError: assert '' == 'drwxr-xr-x' |
| `files.test_files_list::test_file_entry_details` | fail | E       AssertionError: assert '' == '-rw-r--r--' |
| `files.test_files_list::test_mixed_entries` | fail | E       AssertionError: assert '' == 'drwxr-xr-x' |
| `files.test_info::test_file_symlink` | fail | E       AssertionError: assert None == '/home/user/test-simlink-entry/test.txt' |
| `files.test_info::test_get_info_of_directory` | fail | E       AssertionError: assert 0 > 0 |
| `files.test_info::test_get_info_of_file` | fail | E       AssertionError: assert '' == '-rw-r--r--' |
| `files.test_info::test_get_info_of_nonexistent_directory` | fail | E       kosa8.compat.e2b.FileNotFoundException: path /home/user/test_does_not_exist_dir does not exist |
| `files.test_info::test_get_info_of_nonexistent_file` | fail | E       kosa8.compat.e2b.FileNotFoundException: path /home/user/test_does_not_exist.txt does not exist |
| `files.test_metadata::test_metadata_set_via_xattrs_surfaced_in_get_info` | fail | E       AttributeError: 'EntryInfo' object has no attribute 'metadata' |
| `files.test_metadata::test_write_file_without_metadata` | fail | E       AttributeError: 'WriteInfo' object has no attribute 'metadata' |
| `files.test_write::test_write_iterable_of_bytes` | fail | tests/sync/sandbox_sync/files/test_write.py:241: Failed |
| `files.test_write::test_write_iterable_rejects_non_bytes_chunks` | fail | E       AssertionError: Regex pattern did not match. |
| `files.test_write::test_write_multiple_files` | fail | E       AssertionError: assert False |
| `files.test_write::test_write_text_file` | fail | E       AssertionError: assert <FileType.FILE: 'file'> == <FileType.FILE: 'file'> |
| `files.test_write::test_write_with_secured_envd` | fail | E       AttributeError: 'Sandbox' object has no attribute '_envd_access_token' |
| `test_config_propagation::test_connect_sets_stable_host_routing_headers` | fail | E           kosa8.compat.e2b.SandboxException: a kosa8 Cloud API key starts with k8c_live_ (a capability token with k8c_tok_); copy the whole key from https://app.kosa8.com/app#keys |
| `test_config_propagation::test_pause_applies_overrides` | fail | E           AssertionError: Expected 'mock' to have been called once. Called 0 times. |
| `test_config_propagation::test_pause_passes_connection_config_without_overrides` | fail | E           AssertionError: Expected 'mock' to have been called once. Called 0 times. |
| `test_connect::test_connect_in_debug_mode_does_not_call_api` | fail | E           kosa8.compat.e2b.SandboxException: a kosa8 Cloud API key starts with k8c_live_ (a capability token with k8c_tok_); copy the whole key from https://app.kosa8.com/app#keys |
| `test_connect::test_connect_in_env_debug_mode_does_not_call_api` | fail | E           kosa8.compat.e2b.SandboxException: a kosa8 Cloud API key starts with k8c_live_ (a capability token with k8c_tok_); copy the whole key from https://app.kosa8.com/app#keys |
| `test_connect::test_connect_normalizes_unset_tokens` | fail | E           kosa8.compat.e2b.SandboxException: a kosa8 Cloud API key starts with k8c_live_ (a capability token with k8c_tok_); copy the whole key from https://app.kosa8.com/app#keys |
| `test_connect::test_instance_connect_in_debug_mode_does_not_call_api` | fail | E           kosa8.compat.e2b.SandboxException: a kosa8 Cloud API key starts with k8c_live_ (a capability token with k8c_tok_); copy the whole key from https://app.kosa8.com/app#keys |
| `test_create::test_auto_pause_filesystem_only_reboots` | fail | E       AssertionError: assert '' != '' |
| `test_create::test_mode_none_defaults_to_full_memory` | fail | E       AssertionError: assert not True |
| `test_fork::test_fork_killed_sandbox` | fail | E           kosa8.compat.e2b.NotFoundException: no such sandbox [HTTP 404 not_found; request a5f155a36aff7e99-BOS] |
| `test_internet_access::test_internet_access_disabled` | fail | E       kosa8.compat.e2b.CommandExitException: Command exited with code 6 and error: |
| `test_network::test_allow_public_traffic_false` | fail | E       AttributeError: 'Sandbox' object has no attribute 'traffic_access_token' |
| `test_network::test_allow_specific_ip_with_deny_all` | fail | E       kosa8.compat.e2b.CommandExitException: Command exited with code 7 and error: |
| `test_network::test_deny_all_traffic` | fail | E       kosa8.compat.e2b.CommandExitException: Command exited with code 7 and error: |
| `test_network::test_deny_specific_ip` | fail | E       kosa8.compat.e2b.CommandExitException: Command exited with code 7 and error: |
| `test_network::test_domain_allowlist_allows_8888_by_default` | fail | E       AttributeError: 'SandboxInfo' object has no attribute 'network' |
| `test_network::test_empty_dns_servers_allows_no_dns_server` | fail | E       AttributeError: 'SandboxInfo' object has no attribute 'network' |
| `test_network::test_firewall_transform_injects_headers` | fail | E           kosa8.compat.e2b.InvalidArgumentException: there is no template httpbin Templates: base, node, node-8x16, python. GET /v1/images lists them with their images; for an image of your own pass image, or build a template (POST /v1/te |
| `test_network::test_update_network_applies_restrictions` | fail | E       kosa8.compat.e2b.CommandExitException: Command exited with code 7 and error: |
| `test_network::test_update_network_clears_existing_rules` | fail | E       kosa8.compat.e2b.CommandExitException: Command exited with code 7 and error: |
| `test_network::test_update_network_resets_dns_servers_when_omitted` | fail | E       AttributeError: 'SandboxInfo' object has no attribute 'network' |
| `test_secure::test_connect_to_secured` | fail | E       AttributeError: 'Sandbox' object has no attribute '_envd_access_token' |
| `test_secure::test_start_secured` | fail | E       AttributeError: 'Sandbox' object has no attribute '_envd_access_token' |
| `test_timeout::test_shorten_timeout` | fail | tests/sync/sandbox_sync/test_timeout.py:13: AssertionError |
| `commands.test_cmd_list::test_kill_process` | unsupported | E   kosa8.compat.Unsupported: e2b Commands.list is not supported by kosa8. On kosa8: keep the CommandHandle (or its pid) that run(background=True) returned |
| `commands.test_send_stdin::test_close_stdin_via_command_handle` | unsupported | E       kosa8.compat.Unsupported: e2b Commands.run(stdin=True) is not supported by kosa8. On kosa8: kosa8 commands take no stdin; write the input to a file in /workspace |
| `commands.test_send_stdin::test_send_bytes_stdin_to_process` | unsupported | E       kosa8.compat.Unsupported: e2b Commands.run(stdin=True) is not supported by kosa8. On kosa8: kosa8 commands take no stdin; write the input to a file in /workspace |
| `commands.test_send_stdin::test_send_multiline_string_to_process` | unsupported | E       kosa8.compat.Unsupported: e2b Commands.run(stdin=True) is not supported by kosa8. On kosa8: kosa8 commands take no stdin; write the input to a file in /workspace |
| `commands.test_send_stdin::test_send_special_characters_to_process` | unsupported | E       kosa8.compat.Unsupported: e2b Commands.run(stdin=True) is not supported by kosa8. On kosa8: kosa8 commands take no stdin; write the input to a file in /workspace |
| `commands.test_send_stdin::test_send_stdin_to_process` | unsupported | E       kosa8.compat.Unsupported: e2b Commands.run(stdin=True) is not supported by kosa8. On kosa8: kosa8 commands take no stdin; write the input to a file in /workspace |
| `commands.test_send_stdin::test_send_stdin_via_command_handle` | unsupported | E       kosa8.compat.Unsupported: e2b Commands.run(stdin=True) is not supported by kosa8. On kosa8: kosa8 commands take no stdin; write the input to a file in /workspace |
| `commands.test_stream_input::test_stream_input_rejects_single_chunk` | unsupported | E   kosa8.compat.Unsupported: e2b Commands.stream_input is not supported by kosa8. On kosa8: kosa8 commands take no stdin |
| `commands.test_stream_input::test_stream_input_reports_missing_command` | unsupported | E   kosa8.compat.Unsupported: e2b Commands.stream_input is not supported by kosa8. On kosa8: kosa8 commands take no stdin |
| `commands.test_stream_input::test_stream_input_timeout` | unsupported | E       kosa8.compat.Unsupported: e2b Commands.run(stdin=True) is not supported by kosa8. On kosa8: kosa8 commands take no stdin; write the input to a file in /workspace |
| `commands.test_stream_input::test_stream_input_to_command` | unsupported | E       kosa8.compat.Unsupported: e2b Commands.run(stdin=True) is not supported by kosa8. On kosa8: kosa8 commands take no stdin; write the input to a file in /workspace |
| `files.test_metadata::test_metadata_surfaced_after_rename` | unsupported | E       kosa8.compat.Unsupported: e2b Filesystem.write(metadata=...) is not supported by kosa8. On kosa8: kosa8 files carry no metadata; keep it in a sidecar file |
| `files.test_metadata::test_metadata_surfaced_when_listing` | unsupported | E       kosa8.compat.Unsupported: e2b Filesystem.write(metadata=...) is not supported by kosa8. On kosa8: kosa8 files carry no metadata; keep it in a sidecar file |
| `files.test_metadata::test_overwriting_clears_stale_metadata` | unsupported | E       kosa8.compat.Unsupported: e2b Filesystem.write(metadata=...) is not supported by kosa8. On kosa8: kosa8 files carry no metadata; keep it in a sidecar file |
| `files.test_metadata::test_write_file_with_metadata` | unsupported | E       kosa8.compat.Unsupported: e2b Filesystem.write(metadata=...) is not supported by kosa8. On kosa8: kosa8 files carry no metadata; keep it in a sidecar file |
| `files.test_metadata::test_write_file_with_metadata_octet_stream` | unsupported | E       kosa8.compat.Unsupported: e2b Filesystem.write(metadata=...) is not supported by kosa8. On kosa8: kosa8 files carry no metadata; keep it in a sidecar file |
| `files.test_metadata::test_write_files_applies_metadata_to_every_file` | unsupported | E       kosa8.compat.Unsupported: e2b Filesystem.write(metadata=...) is not supported by kosa8. On kosa8: kosa8 files carry no metadata; keep it in a sidecar file |
| `files.test_metadata::test_write_rejects_invalid_metadata` | unsupported | E       kosa8.compat.Unsupported: e2b Filesystem.write(metadata=...) is not supported by kosa8. On kosa8: kosa8 files carry no metadata; keep it in a sidecar file |
| `files.test_secured::test_download_url_with_expired_signing` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.download_url(user=...) is not supported by kosa8. On kosa8: commands run as the image's user; switch user inside the command (su, sudo) |
| `files.test_secured::test_download_url_with_signing` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.download_url(user=...) is not supported by kosa8. On kosa8: commands run as the image's user; switch user inside the command (su, sudo) |
| `files.test_secured::test_download_url_with_signing_and_expiration` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.download_url(user=...) is not supported by kosa8. On kosa8: commands run as the image's user; switch user inside the command (su, sudo) |
| `files.test_watch::test_watch_directory_changes` | unsupported | E   kosa8.compat.Unsupported: e2b Filesystem.watch_dir is not supported by kosa8. On kosa8: poll files.list(path) for changes |
| `files.test_watch::test_watch_directory_changes_with_entry_info` | unsupported | E   kosa8.compat.Unsupported: e2b Filesystem.watch_dir is not supported by kosa8. On kosa8: poll files.list(path) for changes |
| `files.test_watch::test_watch_directory_changes_with_network_mounts_allowed` | unsupported | E   kosa8.compat.Unsupported: e2b Filesystem.watch_dir is not supported by kosa8. On kosa8: poll files.list(path) for changes |
| `files.test_watch::test_watch_file` | unsupported | E   kosa8.compat.Unsupported: e2b Filesystem.watch_dir is not supported by kosa8. On kosa8: poll files.list(path) for changes |
| `files.test_watch::test_watch_file_with_secured_envd` | unsupported | E   kosa8.compat.Unsupported: e2b Filesystem.watch_dir is not supported by kosa8. On kosa8: poll files.list(path) for changes |
| `files.test_watch::test_watch_iterated` | unsupported | E   kosa8.compat.Unsupported: e2b Filesystem.watch_dir is not supported by kosa8. On kosa8: poll files.list(path) for changes |
| `files.test_watch::test_watch_non_existing_directory` | unsupported | E   kosa8.compat.Unsupported: e2b Filesystem.watch_dir is not supported by kosa8. On kosa8: poll files.list(path) for changes |
| `files.test_watch::test_watch_recursive_directory_after_nested_folder_addition` | unsupported | E   kosa8.compat.Unsupported: e2b Filesystem.watch_dir is not supported by kosa8. On kosa8: poll files.list(path) for changes |
| `files.test_watch::test_watch_recursive_directory_changes` | unsupported | E   kosa8.compat.Unsupported: e2b Filesystem.watch_dir is not supported by kosa8. On kosa8: poll files.list(path) for changes |
| `pty.test_pty::test_pty` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.pty is not supported by kosa8. On kosa8: sandbox.kosa8.terminal() gives a wss:// URL for an interactive terminal |
| `pty.test_pty_connect::test_connect_to_pty` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.pty is not supported by kosa8. On kosa8: sandbox.kosa8.terminal() gives a wss:// URL for an interactive terminal |
| `pty.test_pty_kill::test_kill_non_existing_pty` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.pty is not supported by kosa8. On kosa8: sandbox.kosa8.terminal() gives a wss:// URL for an interactive terminal |
| `pty.test_pty_kill::test_kill_pty` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.pty is not supported by kosa8. On kosa8: sandbox.kosa8.terminal() gives a wss:// URL for an interactive terminal |
| `pty.test_resize::test_resize` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.pty is not supported by kosa8. On kosa8: sandbox.kosa8.terminal() gives a wss:// URL for an interactive terminal |
| `pty.test_send_input::test_send_input` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.pty is not supported by kosa8. On kosa8: sandbox.kosa8.terminal() gives a wss:// URL for an interactive terminal |
| `pty.test_stream_input::test_stream_input_rejects_single_chunk` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.pty is not supported by kosa8. On kosa8: sandbox.kosa8.terminal() gives a wss:// URL for an interactive terminal |
| `pty.test_stream_input::test_stream_input_to_pty` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.pty is not supported by kosa8. On kosa8: sandbox.kosa8.terminal() gives a wss:// URL for an interactive terminal |
| `test_create::test_auto_resume_wakes_on_http_request` | unsupported | E               kosa8.compat.Unsupported: e2b Sandbox.create(lifecycle={'auto_resume': True}) is not supported by kosa8. On kosa8: a kosa8 sandbox wakes for the next command by itself; leave auto_resume out |
| `test_create::test_mcp_gateway_start_failure_kills_created_sandbox` | unsupported | E       kosa8.compat.Unsupported: e2b Sandbox.create(mcp=...) is not supported by kosa8. On kosa8: run the MCP server yourself with commands.run(..., background=True) |
| `test_metrics::test_sbx_metrics` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.get_metrics is not supported by kosa8. On kosa8: sandbox.kosa8.metrics() (kosa8's own fields) |
| `test_metrics::test_sbx_metrics_time_range` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.get_metrics is not supported by kosa8. On kosa8: sandbox.kosa8.metrics() (kosa8's own fields) |
| `test_network::test_dns_servers_allows_listed_servers_instead_of_8888` | unsupported | E       kosa8.compat.Unsupported: e2b Sandbox.create(network={'dns_servers': ...}) is not supported by kosa8. On kosa8: leave it out; kosa8 resolves allowed names for the sandbox |
| `test_network::test_https_ports` | unsupported | E       kosa8.compat.Unsupported: e2b Sandbox.create(network={'https_ports': ...}) is not supported by kosa8. On kosa8: a preview speaks HTTPS to the caller and plain HTTP to the port; serve plain HTTP inside |
| `test_network::test_mask_request_host` | unsupported | E       kosa8.compat.Unsupported: e2b Sandbox.create(network={'mask_request_host': ...}) is not supported by kosa8. On kosa8: previews are served at their own host: sandbox.get_host(port) |
| `test_snapshot_api::test_create_filesystem_only_snapshot` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.create_snapshot(mode=...) is not supported by kosa8. On kosa8: kosa8 snapshots always keep memory and disk |
| `test_snapshot_filesystem_only::test_pause_filesystem_only` | unsupported | E   kosa8.compat.Unsupported: e2b Sandbox.pause(filesystem only) is not supported by kosa8. On kosa8: pause() keeps memory and disk; delete the sandbox if you need neither |
| `commands.test_cmd_connect::test_connect_to_non_existing_process` | pass |  |
| `commands.test_cmd_connect::test_connect_to_process` | pass |  |
| `commands.test_cmd_connect::test_connect_without_retaining_output` | pass |  |
| `commands.test_cmd_kill::test_kill_non_existing_process` | pass |  |
| `commands.test_cmd_kill::test_kill_process` | pass |  |
| `commands.test_env_vars::test_command_envs` | pass |  |
| `commands.test_env_vars::test_python_command_scoped_env_vars` | pass |  |
| `commands.test_env_vars::test_sandbox_envs` | pass |  |
| `commands.test_run::test_run` | pass |  |
| `commands.test_run::test_run_in_background_without_retaining_output` | pass |  |
| `commands.test_run::test_run_with_custom_shell` | pass |  |
| `commands.test_run::test_run_with_empty_shell` | pass |  |
| `commands.test_run::test_run_with_multiline_string` | pass |  |
| `commands.test_run::test_run_with_non_sequence_shell` | pass |  |
| `commands.test_run::test_run_with_shell_without_args` | pass |  |
| `commands.test_run::test_run_with_special_characters` | pass |  |
| `commands.test_run::test_run_with_string_shell` | pass |  |
| `commands.test_run::test_run_with_timeout` | pass |  |
| `commands.test_run::test_run_with_too_short_timeout` | pass |  |
| `commands.test_run::test_run_without_retaining_output` | pass |  |
| `commands.test_stream_input::test_stream_input_orders_start_and_data` | pass |  |
| `files.test_content_encoding::test_read_bytes_with_gzip` | pass |  |
| `files.test_content_encoding::test_write_and_read_with_gzip` | pass |  |
| `files.test_content_encoding::test_write_files_with_gzip` | pass |  |
| `files.test_content_encoding::test_write_gzip_read_plain` | pass |  |
| `files.test_exists::test_exists` | pass |  |
| `files.test_files_list::test_list_directory` | pass |  |
| `files.test_files_list::test_list_directory_error_cases` | pass |  |
| `files.test_make_dir::test_make_directory` | pass |  |
| `files.test_make_dir::test_make_directory_already_exists` | pass |  |
| `files.test_make_dir::test_make_nested_directory` | pass |  |
| `files.test_read::test_read_empty_file` | pass |  |
| `files.test_read::test_read_file` | pass |  |
| `files.test_read::test_read_file_as_stream` | pass |  |
| `files.test_read::test_read_file_as_stream_context_manager` | pass |  |
| `files.test_read::test_read_file_as_stream_partial_then_close` | pass |  |
| `files.test_read::test_read_file_as_stream_with_gzip` | pass |  |
| `files.test_read::test_read_non_existing_file` | pass |  |
| `files.test_read::test_read_non_existing_file_as_stream` | pass |  |
| `files.test_read::test_read_non_existing_file_catches_with_deprecated_not_found_exception` | pass |  |
| `files.test_remove::test_remove_file` | pass |  |
| `files.test_remove::test_remove_non_existing_file` | pass |  |
| `files.test_rename::test_rename_file` | pass |  |
| `files.test_rename::test_rename_non_existing_file` | pass |  |
| `files.test_write::test_overwrite_file` | pass |  |
| `files.test_write::test_write_binary_file` | pass |  |
| `files.test_write::test_write_file_like_wrapper[False]` | pass |  |
| `files.test_write::test_write_file_like_wrapper[True]` | pass |  |
| `files.test_write::test_write_files_with_different_data_types` | pass |  |
| `files.test_write::test_write_io_with_octet_stream` | pass |  |
| `files.test_write::test_write_io_with_octet_stream_and_gzip` | pass |  |
| `files.test_write::test_write_text_file_like_wrapper[False]` | pass |  |
| `files.test_write::test_write_text_file_like_wrapper[True]` | pass |  |
| `files.test_write::test_write_text_io_with_octet_stream` | pass |  |
| `files.test_write::test_write_to_non_existing_directory` | pass |  |
| `test_connect::test_connect` | pass |  |
| `test_connect::test_connect_extends_timeout_on_running_sandbox` | pass |  |
| `test_connect::test_connect_to_paused_sandbox_resumes` | pass |  |
| `test_connect::test_connect_with_secure` | pass |  |
| `test_connect::test_resume_does_not_shorten_timeout_on_running_sandbox` | pass |  |
| `test_create::test_auto_pause_without_auto_resume_requires_connect` | pass |  |
| `test_create::test_invalid_on_timeout_type_does_not_crash` | pass |  |
| `test_create::test_metadata` | pass |  |
| `test_create::test_start` | pass |  |
| `test_fork::test_fork` | pass |  |
| `test_fork::test_fork_by_id` | pass |  |
| `test_fork::test_fork_multiple` | pass |  |
| `test_host::test_ping_server` | pass |  |
| `test_internet_access::test_internet_access_default` | pass |  |
| `test_internet_access::test_internet_access_enabled` | pass |  |
| `test_kill::test_kill` | pass |  |
| `test_network::test_allow_public_traffic_true` | pass |  |
| `test_network::test_allow_takes_precedence_over_deny` | pass |  |
| `test_network::test_wait_for_status_retries_transport_errors` | pass |  |
| `test_snapshot::test_snapshot` | pass |  |
| `test_snapshot_api::test_create_multiple_sandboxes_from_snapshot` | pass |  |
| `test_snapshot_api::test_create_named_snapshot` | pass |  |
| `test_snapshot_api::test_create_sandbox_from_snapshot` | pass |  |
| `test_snapshot_api::test_create_snapshot` | pass |  |
| `test_snapshot_api::test_create_snapshot_class_method` | pass |  |
| `test_snapshot_api::test_delete_snapshot` | pass |  |
| `test_snapshot_api::test_list_snapshots` | pass |  |
| `test_snapshot_api::test_list_snapshots_filtered_by_name` | pass |  |
| `test_snapshot_api::test_list_snapshots_for_sandbox` | pass |  |
| `test_snapshot_api::test_snapshot_preserves_filesystem` | pass |  |
| `test_sync_client_lifecycle::test_sync_filesystem_clients_are_shared_across_threads` | pass |  |
| `test_sync_client_lifecycle::test_sync_sandbox_shares_one_envd_api_across_modules` | pass |  |
| `test_timeout::test_get_timeout` | pass |  |
| `test_timeout::test_shorten_then_lengthen_timeout` | pass |  |

## Daytona Python SDK 2.1.0

| Case | Outcome | Why |
|---|---|---|
| `declarative-image/main.py` | fail | pydantic_core._pydantic_core.ValidationError: 1 validation error for CreateSnapshotParams |
| `auto-delete/auto_delete.py` | unsupported | kosa8.compat.Unsupported: daytona Sandbox.set_auto_delete_interval is not supported by kosa8. On kosa8: set_ttl(minutes): kosa8 deletes on a lifetime, not after a stop |
| `charts/main.py` | unsupported | kosa8.compat.Unsupported: daytona create(image=Image...) (a declarative image build) is not supported by kosa8. On kosa8: build and push the image, then image='registry/name:tag' |
| `exec-command/exec.py` | unsupported | kosa8.compat.Unsupported: daytona Sandbox.code_interpreter is not supported by kosa8. On kosa8: sandbox.kosa8.run_code(code, context=...) (stateful Python cells) |
| `exec-command/exec_linked.py` | unsupported | kosa8.compat.Unsupported: daytona create(CreateSandboxFromSnapshotParams(ephemeral=...)) is not supported by kosa8. On kosa8: ttl_minutes, or delete() when done |
| `file-operations/main.py` | unsupported | kosa8.compat.Unsupported: daytona FileSystem.search_files is not supported by kosa8. On kosa8: process.exec("find PATH -name 'GLOB'") |
| `git-lsp/main.py` | unsupported | kosa8.compat.Unsupported: daytona create(image=Image...) (a declarative image build) is not supported by kosa8. On kosa8: build and push the image, then image='registry/name:tag' |
| `volumes/volume.py` | unsupported | kosa8.compat.Unsupported: daytona Daytona.volume is not supported by kosa8. On kosa8: keep data in /workspace, or snapshot the sandbox |
| `auto-archive/auto_archive.py` | pass |  |
| `exec-command/exec_logs_async.py` | pass |  |
| `exec-command/exec_session.py` | pass |  |
| `lifecycle/lifecycle.py` | pass |  |
| `network-settings/main.py` | pass |  |
| `pagination/sandbox.py` | pass |  |
| `pagination/snapshot.py` | pass |  |
| `pty/main.py` | pass |  |
| `region/main.py` | pass |  |

## Modal Python SDK 1.6.4

| Case | Outcome | Why |
|---|---|---|
| `harbor_evals.py` | fail | FileNotFoundError: [Errno 2] No such file or directory: 'harbor' |
| `sandbox_secret_injection.py` | fail | AttributeError: type object 'Secret' has no attribute 'objects' |
| `sidecar_agent.py` | fail | subprocess.CalledProcessError: Command '['python', '-m', 'modal', 'endpoint', 'create', '--name', 'example-sidecar-agent', '--model' |
| `sidecar_secrets_injection.py` | fail | AttributeError: type object 'Secret' has no attribute 'objects' |
| `jupyter_sandbox.py` | unsupported | kosa8.compat.Unsupported: modal Image.uv_pip_install is not supported by kosa8. On kosa8: kosa8 does not build images: build and push one with these steps, then Image.from_registry('registry/name:tag'); or set a sandbox up once and reuse it |
| `safe_code_execution.py` | unsupported | kosa8.compat.Unsupported: modal Image.apt_install is not supported by kosa8. On kosa8: kosa8 does not build images: build and push one with these steps, then Image.from_registry('registry/name:tag'); or set a sandbox up once and reuse it wi |
| `sandbox_agent.py` | unsupported | kosa8.compat.Unsupported: modal Image.apt_install is not supported by kosa8. On kosa8: kosa8 does not build images: build and push one with these steps, then Image.from_registry('registry/name:tag'); or set a sandbox up once and reuse it wi |
| `sandbox_pool.py` | unsupported | kosa8.compat.Unsupported: modal Image.uv_pip_install is not supported by kosa8. On kosa8: kosa8 does not build images: build and push one with these steps, then Image.from_registry('registry/name:tag'); or set a sandbox up once and reuse it |
| `sidecar_redis.py` | unsupported | kosa8.compat.Unsupported: modal Image.uv_pip_install is not supported by kosa8. On kosa8: kosa8 does not build images: build and push one with these steps, then Image.from_registry('registry/name:tag'); or set a sandbox up once and reuse it |
| `sidecar_traffic_routing.py` | unsupported | kosa8.compat.Unsupported: modal Image.uv_pip_install is not supported by kosa8. On kosa8: kosa8 does not build images: build and push one with these steps, then Image.from_registry('registry/name:tag'); or set a sandbox up once and reuse it |
| `simple_code_interpreter.py` | unsupported | kosa8.compat.Unsupported: modal StreamWriter.write (stdin) is not supported by kosa8. On kosa8: kosa8 commands take no stdin; write the input to a file in /workspace and read it |
