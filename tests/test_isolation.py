import io
import json
import os
import signal
import stat
import sys
import tarfile
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "evals"))

import isolation  # noqa: E402


AGENT_IMAGE = "sha256:" + "a" * 64
PROXY_IMAGE = "sha256:" + "b" * 64
SESSION_ID = "019cc483-7f50-7f12-8abc-0123456789ab"


def tar_bytes(entries=()):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, kind, content in entries:
            info = tarfile.TarInfo(name)
            info.uid = 0
            info.gid = 0
            info.mtime = 0
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o700
                archive.addfile(info)
            elif kind == "file":
                payload = bytes(content)
                info.type = tarfile.REGTYPE
                info.mode = 0o600
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = str(content)
                archive.addfile(info)
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = str(content)
                archive.addfile(info)
            else:
                raise AssertionError(kind)
    return output.getvalue()


def runtime_record():
    identity = lambda path, kind: {  # noqa: E731
        "path": path,
        "kind": kind,
        "device": 1,
        "inode": 2,
        "mode": 0o700,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "size": 1,
        "mtime_ns": 1,
        "ctime_ns": 1,
        **({"sha256": "f" * 64} if kind != "socket" else {}),
    }
    return {
        "contract": "docker-agent-proxy-v1",
        "roots": {"state": {"path": "/trusted/evaluator-state"}},
        "trust": {
            "docker_executable": identity("/trusted/docker", "executable"),
            "docker_socket": identity("/trusted/docker.sock", "socket"),
            "seccomp_profile": identity("/trusted/seccomp.json", "regular"),
        },
        "images": {
            "agent": {
                "image_id": AGENT_IMAGE,
                "user": "10001:10001",
            },
            "proxy": {
                "image_id": PROXY_IMAGE,
                "user": "10001:10001",
            },
        },
        "codex_version": "codex-cli 0.149.1",
        "preflight": {
            "sandbox": {"ok": True, "codex_exec_config_parsed": True},
            "boundary": {"ok": True},
        },
    }


class SnapshotTests(unittest.TestCase):
    def test_prepare_snapshots_creates_exact_empty_external_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run = root / "results" / "run"
            state = root / "state" / "run"
            run.mkdir(parents=True)
            run.chmod(0o700)
            state.mkdir(parents=True, mode=0o700)
            state.chmod(0o700)

            prepared = isolation.prepare_snapshots(run, state)

            self.assertEqual({"workspace", "state", "journal"}, set(prepared))
            workspace = Path(prepared["workspace"])
            codex_home = Path(prepared["state"])
            self.assertEqual(run / "snapshots" / "workspace-generations", workspace.parent)
            self.assertEqual(state / "codex-home-generations", codex_home.parent)
            self.assertRegex(workspace.name, r"^[0-9a-f]{64}$")
            self.assertEqual(workspace.name, codex_home.name)
            self.assertEqual(run / "snapshots" / "current.json", Path(prepared["journal"]))
            journal = isolation._load_snapshot_journal(Path(prepared["journal"]))
            self.assertEqual(0, journal["turn"])
            self.assertEqual([], list(workspace.iterdir()))
            self.assertEqual([], list(codex_home.iterdir()))
            self.assertFalse(workspace.is_symlink())
            self.assertFalse(codex_home.is_symlink())
            with self.assertRaises(isolation.IsolationError):
                isolation.prepare_snapshots(run, state)

    def test_prepare_rejects_symlinked_or_overlapping_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run = root / "run"
            run.mkdir()
            run.chmod(0o700)
            nested = run / "state"
            nested.mkdir(mode=0o700)
            with self.assertRaisesRegex(isolation.IsolationError, "external"):
                isolation.prepare_snapshots(run, nested)

            outside = root / "outside"
            outside.mkdir(mode=0o700)
            linked = root / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(isolation.IsolationError):
                isolation.prepare_snapshots(run, linked)

    def test_host_snapshot_rejects_links_specials_and_casefold_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "target").write_text("x", encoding="utf-8")
            (root / "link").symlink_to("target")
            with self.assertRaisesRegex(isolation.IsolationError, "symlink"):
                isolation._snapshot_archive(root, max_bytes=1024)
            (root / "link").unlink()

            os.link(root / "target", root / "hard")
            with self.assertRaisesRegex(isolation.IsolationError, "hard-linked"):
                isolation._snapshot_archive(root, max_bytes=1024)
            (root / "hard").unlink()
            (root / "target").unlink()

            (root / "Name").write_text("a", encoding="utf-8")
            (root / "name").write_text("b", encoding="utf-8")
            if (root / "Name").lstat().st_ino == (root / "name").lstat().st_ino:
                self.assertEqual(
                    isolation._canonical_name("Name")[1],
                    isolation._canonical_name("name")[1],
                )
            else:
                with self.assertRaisesRegex(isolation.IsolationError, "casefold"):
                    isolation._snapshot_archive(root, max_bytes=1024)

    def test_tar_validation_rejects_unsafe_members_duplicates_and_secrets(self):
        secret = bytearray(b"dedicated-secret-value")
        cases = (
            tar_bytes((("../escape", "file", b"x"),)),
            tar_bytes((("link", "symlink", "target"),)),
            tar_bytes((("hard", "hardlink", "target"),)),
            tar_bytes((("Name", "file", b"a"), ("name", "file", b"b"))),
            tar_bytes((("unsafe\nname", "file", b"x"),)),
            tar_bytes((("safe", "file", bytes(secret)),)),
        )
        for archive in cases:
            with self.subTest(size=len(archive)):
                with self.assertRaises(isolation.IsolationError):
                    isolation._validated_archive_entries(
                        archive, max_bytes=1024, secrets_=(secret,)
                    )

    def test_pair_commit_keeps_old_pointer_when_publish_faults(self):
        after = tar_bytes((("after", "file", b"new"),))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run = root / "run"
            state = root / "state"
            run.mkdir(mode=0o700)
            state.mkdir(mode=0o700)
            prepared = isolation.prepare_snapshots(run, state)
            journal = Path(prepared["journal"])
            before = isolation._load_snapshot_journal(journal)
            with mock.patch.object(
                isolation,
                "_atomic_journal_write",
                side_effect=isolation.IsolationError("fault before pointer replace"),
            ):
                with self.assertRaisesRegex(isolation.IsolationError, "fault"):
                    isolation._commit_snapshot_pair(
                        journal,
                        Path(prepared["workspace"]),
                        Path(prepared["state"]),
                        after,
                        after,
                        turn=1,
                        session_id=SESSION_ID,
                        secrets_=(),
                    )
            self.assertEqual(before, isolation._load_snapshot_journal(journal))

    def test_pair_commit_publishes_one_matching_turn_and_session(self):
        workspace_after = tar_bytes((("diagram.svg", "file", b"<svg/>"),))
        state_after = tar_bytes((("sessions", "dir", b""),))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run = root / "run"
            state = root / "state"
            run.mkdir(mode=0o700)
            state.mkdir(mode=0o700)
            prepared = isolation.prepare_snapshots(run, state)
            committed = isolation._commit_snapshot_pair(
                Path(prepared["journal"]),
                Path(prepared["workspace"]),
                Path(prepared["state"]),
                workspace_after,
                state_after,
                turn=1,
                session_id=SESSION_ID,
                secrets_=(),
            )
            journal = isolation._load_snapshot_journal(Path(prepared["journal"]))
            self.assertEqual(1, journal["turn"])
            self.assertEqual(SESSION_ID, journal["session_id"])
            self.assertEqual(committed["workspace"]["path"], journal["workspace"]["path"])
            self.assertEqual(committed["state"]["path"], journal["state"]["path"])
            self.assertTrue((Path(journal["workspace"]["path"]) / "diagram.svg").is_file())

    def test_pair_commit_redurabilizes_visible_orphans_before_pointer_publish(self):
        after = tar_bytes((("after", "file", b"new"),))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            run = root / "run"
            state = root / "state"
            run.mkdir(mode=0o700)
            state.mkdir(mode=0o700)
            prepared = isolation.prepare_snapshots(run, state)
            isolation._materialize_snapshot_generation(
                Path(prepared["workspace"]).parent,
                after,
                root=isolation.CONTAINER_WORKSPACE,
                max_bytes=isolation.WORKSPACE_LIMIT_BYTES,
                secrets_=(),
            )
            isolation._materialize_snapshot_generation(
                Path(prepared["state"]).parent,
                after,
                root=isolation.CONTAINER_CODEX_HOME,
                max_bytes=isolation.STATE_LIMIT_BYTES,
                secrets_=(),
            )
            events = []
            original_tree = isolation._fsync_snapshot_tree
            original_directory = isolation._fsync_directory
            original_journal = isolation._atomic_journal_write

            def fsync_tree(path):
                events.append(("tree", str(path)))
                return original_tree(path)

            def fsync_directory(path):
                events.append(("directory", str(path)))
                return original_directory(path)

            def write_journal(path, pointer):
                events.append(("journal", str(path)))
                return original_journal(path, pointer)

            with mock.patch.object(
                isolation, "_fsync_snapshot_tree", side_effect=fsync_tree
            ), mock.patch.object(
                isolation, "_fsync_directory", side_effect=fsync_directory
            ), mock.patch.object(
                isolation, "_atomic_journal_write", side_effect=write_journal
            ):
                isolation._commit_snapshot_pair(
                    Path(prepared["journal"]),
                    Path(prepared["workspace"]),
                    Path(prepared["state"]),
                    after,
                    after,
                    turn=1,
                    session_id=SESSION_ID,
                    secrets_=(),
                )
            journal_index = next(
                index for index, event in enumerate(events) if event[0] == "journal"
            )
            before_journal = events[:journal_index]
            self.assertEqual(
                2, len([event for event in before_journal if event[0] == "tree"])
            )
            self.assertTrue(
                {
                    str(Path(prepared["workspace"]).parent),
                    str(Path(prepared["state"]).parent),
                }.issubset(
                    {
                        event[1]
                        for event in before_journal
                        if event[0] == "directory"
                    }
                )
            )

    def test_snapshot_policy_rejects_resume_created_skill_layers(self):
        workspace = tar_bytes(((".agents/skills/evil/SKILL.md", "file", b"attack"),))
        state = tar_bytes((("skills/evil/SKILL.md", "file", b"attack"),))
        with self.assertRaisesRegex(isolation.IsolationError, "skill"):
            isolation._validated_snapshot_archive(
                workspace,
                root=isolation.CONTAINER_WORKSPACE,
                max_bytes=1024,
                secrets_=(),
            )
        with self.assertRaisesRegex(isolation.IsolationError, "skill"):
            isolation._validated_snapshot_archive(
                state,
                root=isolation.CONTAINER_CODEX_HOME,
                max_bytes=1024,
                secrets_=(),
            )

    def test_turn_context_refuses_journal_state_mismatch_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            results = root / "results"
            state_root = root / "state"
            run = results / "experiment" / "run"
            state_dir = state_root / "experiment" / "run"
            for path in (results, state_root, run, state_dir):
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.chmod(0o700)
            prepared = isolation.prepare_snapshots(run, state_dir)
            (run / "input" / "repo").mkdir(mode=0o700, parents=True)
            (run / "raw").mkdir(mode=0o700)
            runtime = runtime_record()
            runtime["roots"] = {
                "repository": {"path": str(isolation.REPOSITORY_ROOT)},
                "results": {"path": str(results)},
                "state": {"path": str(state_root)},
            }
            state = {
                "condition": "baseline",
                "model": "gpt-5.4",
                "run_id": "run",
                "turn_count": 0,
                "session_id": None,
                "execution": {
                    "isolation": {
                        "runtime_validation": runtime,
                        "workspace_snapshot_path": prepared["workspace"],
                        "state_snapshot_path": prepared["state"],
                        "snapshot_journal_path": prepared["journal"],
                    }
                },
            }
            journal = isolation._load_snapshot_journal(Path(prepared["journal"]))
            journal["turn"] = 1
            journal["generation"] = 1
            journal["session_id"] = SESSION_ID
            isolation._atomic_journal_write(Path(prepared["journal"]), journal)
            with mock.patch.object(isolation, "_revalidate_runtime"), mock.patch.object(
                isolation, "_revalidate_directory_identity"
            ):
                with self.assertRaisesRegex(isolation.IsolationError, "turn/session"):
                    isolation._turn_context(run, state, "prompt", 1, True, 30)


class CommandBoundaryTests(unittest.TestCase):
    @staticmethod
    def _passwd(name, uid, gid):
        return mock.Mock(pw_name=name, pw_uid=uid, pw_gid=gid)

    def _group_trust(self, group, users, explicit, *, current_uid=501):
        with mock.patch.object(isolation.os, "geteuid", return_value=current_uid), mock.patch.object(
            isolation.grp, "getgrgid", return_value=group
        ), mock.patch.object(
            isolation.pwd, "getpwall", return_value=users
        ), mock.patch.object(
            isolation.pwd, "getpwnam", side_effect=explicit.__getitem__
        ):
            return isolation._group_is_trusted(80)

    def test_trusted_group_accepts_duplicate_directory_records_with_allowed_uids(self):
        group = mock.Mock(gr_gid=80, gr_mem=("current",))
        users = [
            self._passwd("current", 501, 20),
            self._passwd("current", 0, 20),
            self._passwd("unrelated", 700, 20),
        ]
        explicit = {"current": self._passwd("current", 501, 20)}
        self.assertTrue(self._group_trust(group, users, explicit))

    def test_trusted_group_rejects_untrusted_duplicate_explicit_member_uid(self):
        group = mock.Mock(gr_gid=80, gr_mem=("current",))
        users = [
            self._passwd("current", 501, 20),
            self._passwd("current", 502, 20),
        ]
        explicit = {"current": self._passwd("current", 501, 20)}
        self.assertFalse(self._group_trust(group, users, explicit))

    def test_trusted_group_rejects_untrusted_primary_uid_and_wrong_group_record(self):
        explicit = {"current": self._passwd("current", 501, 20)}
        cases = (
            (
                mock.Mock(gr_gid=80, gr_mem=("current",)),
                [self._passwd("current", 501, 20), self._passwd("attacker", 502, 80)],
            ),
            (mock.Mock(gr_gid=81, gr_mem=("current",)), [],),
        )
        for group, users in cases:
            with self.subTest(returned_gid=group.gr_gid, users=len(users)):
                self.assertFalse(self._group_trust(group, users, explicit))

    def test_trusted_group_member_lookup_failure_is_fail_closed(self):
        group = mock.Mock(gr_gid=80, gr_mem=("missing",))
        with mock.patch.object(isolation.os, "geteuid", return_value=501), mock.patch.object(
            isolation.grp, "getgrgid", return_value=group
        ), mock.patch.object(
            isolation.pwd,
            "getpwall",
            return_value=[self._passwd("current", 501, 80)],
        ), mock.patch.object(
            isolation.pwd, "getpwnam", side_effect=KeyError("missing")
        ):
            self.assertFalse(isolation._group_is_trusted(80))

    def test_macos_acl_parser_allows_only_the_standard_deny_delete_entry(self):
        plain = "drwxr-xr-x  5 root wheel 160 Jan 1 00:00 /trusted\n"
        standard = plain + " 0: group:everyone deny delete\n"
        self.assertTrue(isolation._darwin_acl_output_is_trusted(plain, 1))
        self.assertTrue(isolation._darwin_acl_output_is_trusted(standard, 1))

        unsafe_outputs = (
            plain + " 0: user:attacker allow write\n",
            plain + " 0: group:everyone deny delete\n 1: user:attacker allow write\n",
            plain + " malformed ACL entry\n",
            plain + " 0: group:everyone deny delete,write\n",
            standard + "\n",
        )
        for output in unsafe_outputs:
            with self.subTest(output=output):
                self.assertFalse(isolation._darwin_acl_output_is_trusted(output, 1))

    def test_macos_acl_parser_counts_each_inspected_path(self):
        first = "drwx------  2 user staff 64 Jan 1 00:00 /first\n"
        second = "-rw-r--r--  1 user staff 1 Jan 1 00:00 /second\n"
        self.assertTrue(isolation._darwin_acl_output_is_trusted(first + second, 2))
        self.assertFalse(isolation._darwin_acl_output_is_trusted(first, 2))

    def test_macos_case_aliases_overlap_and_are_not_exact_paths(self):
        canonical = Path("/Users")
        alias = Path("/users")
        if not canonical.exists() or not alias.exists() or not canonical.samefile(alias):
            self.skipTest("filesystem has no /Users case alias")
        self.assertTrue(isolation._paths_overlap(canonical, alias))
        with self.assertRaisesRegex(isolation.IsolationError, "alias"):
            isolation._exact_existing_path(alias, "case path")

    def test_agent_command_has_only_nested_readonly_mounts_and_capped_noexec_tmpfs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            skill = root / "skill"
            repo.mkdir()
            skill.mkdir()
            arguments = isolation._agent_container_arguments(
                runtime_record(),
                name="agent",
                network="private",
                repo=repo,
                skill=skill,
            )
        rendered = "\n".join(arguments)
        self.assertIn("--read-only", arguments)
        self.assertIn("--cap-drop", arguments)
        self.assertIn("no-new-privileges", arguments)
        self.assertIn("systempaths=unconfined", rendered)
        self.assertEqual(3, arguments.count("--tmpfs"))
        self.assertEqual(3, rendered.count("noexec"))
        self.assertEqual(2, arguments.count("--mount"))
        self.assertIn("bind-recursive=readonly", rendered)
        self.assertEqual(2, rendered.count("bind-propagation=rprivate"))
        self.assertIn("/workspace/repo", rendered)
        self.assertIn("/codex-home/skills/diagram-grill", rendered)
        for forbidden in ("auth.json", "docker.sock", "/raw/", "oracles"):
            self.assertNotIn(forbidden, rendered)

    def test_baseline_has_no_skill_mount_and_proxy_has_no_secret_or_host_port(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve() / "repo"
            repo.mkdir()
            agent = isolation._agent_container_arguments(
                runtime_record(),
                name="agent",
                network="private",
                repo=repo,
                skill=None,
            )
        proxy = isolation._proxy_create_arguments(
            runtime_record(), name="proxy", network="egress", model="gpt-5.4"
        )
        self.assertEqual(1, agent.count("--mount"))
        self.assertNotIn(isolation.CONTAINER_SKILL, "\n".join(agent))
        self.assertIn("--network-alias", proxy)
        self.assertIn("eval-proxy", proxy)
        self.assertNotIn("--publish", proxy)
        for argument in proxy:
            self.assertNotIn("sk-", argument)
            self.assertNotIn("credential", argument.lower())

    def test_codex_config_is_strict_credential_free_and_disables_top_level_web(self):
        arguments = isolation._codex_exec_arguments(
            model="gpt-5.4",
            base_url="http://eval-proxy:8080/v1",
            capability="c" * 64,
            last_message="/tmp/last.txt",
            initial=True,
            session_id=None,
        )
        rendered = "\n".join(arguments)
        self.assertIn("--strict-config", arguments)
        self.assertIn("--ignore-user-config", arguments)
        self.assertIn("--ignore-rules", arguments)
        self.assertIn('web_search="disabled"', rendered)
        self.assertNotIn("features.web_search", rendered)
        self.assertIn("sandbox_workspace_write.network_access=false", rendered)
        self.assertIn('shell_environment_policy.inherit="none"', rendered)
        self.assertIn("shell_environment_policy.set=", rendered)
        self.assertIn("model_providers.eval_proxy.requires_openai_auth=false", rendered)
        self.assertIn(isolation.CAPABILITY_HEADER, rendered)
        self.assertIn('model_reasoning_effort="medium"', rendered)
        self.assertIn('service_tier="default"', rendered)
        self.assertIn('approval_policy="never"', rendered)
        self.assertNotRegex(rendered, r"API|TOKEN|AUTHORIZATION")

    def test_resource_names_are_unique_per_turn_nonce(self):
        first = isolation._resource_name("run", 1, "agent", "a" * 12)
        second = isolation._resource_name("run", 1, "agent", "b" * 12)
        proxy = isolation._resource_name("run", 1, "proxy", "a" * 12)
        self.assertEqual(3, len({first, second, proxy}))
        self.assertTrue(all(len(value) <= 63 for value in (first, second, proxy)))

    def test_interactive_agent_exec_keeps_stdin_open(self):
        arguments = isolation._agent_exec_arguments(
            runtime_record(), "agent", ("codex", "exec", "-"), interactive=True
        )
        self.assertEqual("--interactive", arguments[1])
        self.assertEqual(["codex", "exec", "-"], arguments[-3:])

    def test_internal_network_is_labeled_and_uses_isolated_gateways(self):
        observed = []

        def capture(_runtime, arguments, **_kwargs):
            observed.append(list(arguments))
            return isolation.Capture(0, b"a" * 64 + b"\n", b"")

        with mock.patch.object(isolation, "_docker_capture", side_effect=capture):
            isolation._create_network(runtime_record(), "private", internal=True)
        command = observed[0]
        self.assertIn(isolation.RESOURCE_LABEL, command)
        self.assertIn("--internal", command)
        self.assertIn("com.docker.network.bridge.gateway_mode_ipv4=isolated", command)
        self.assertIn("com.docker.network.bridge.gateway_mode_ipv6=isolated", command)

    def test_proxy_starts_on_internal_network_then_adds_egress(self):
        calls = []

        def capture(_runtime, arguments, **_kwargs):
            calls.append(list(arguments))
            return isolation.Capture(0, b"", b"")

        with mock.patch.object(isolation, "_docker_capture", side_effect=capture), mock.patch.object(
            isolation, "_docker_start", return_value="managed"
        ), mock.patch.object(isolation, "_wait_for_input"):
            result = isolation._start_proxy(
                runtime_record(),
                name="proxy",
                egress_network="egress",
                internal_network="internal",
                model="gpt-5.4",
                credential=bytearray(b"x" * 24),
                sentinel=bytearray(b"s" * 64),
                capability=bytearray(b"c" * 64),
            )
        self.assertEqual("managed", result)
        self.assertEqual("internal", calls[0][calls[0].index("--network") + 1])
        self.assertEqual(["network", "connect", "egress", "proxy"], calls[1])
        self.assertEqual(isolation.CONTAINER_PROXY_HOST, calls[0][calls[0].index("--host") + 1])

    def test_import_bridge_has_only_the_runtime_importer_buffer(self):
        self.assertNotIn("TemporaryFile", isolation._IMPORT_BRIDGE_PROGRAM)
        self.assertIn("sys.stdin.buffer", isolation._IMPORT_BRIDGE_PROGRAM)

    def test_scavenger_removes_labeled_containers_before_networks(self):
        order = []
        with mock.patch.object(
            isolation,
            "_labeled_resource_ids",
            side_effect=(["a" * 64], ["b" * 64]),
        ), mock.patch.object(
            isolation,
            "_remove_container",
            side_effect=lambda _runtime, value: order.append(("container", value)) or {"succeeded": True},
        ), mock.patch.object(
            isolation,
            "_remove_network",
            side_effect=lambda _runtime, value: order.append(("network", value)) or {"succeeded": True},
        ):
            isolation._scavenge_resources(runtime_record())
        self.assertEqual(["container", "network"], [kind for kind, _ in order])

    def test_signal_handlers_latch_once_and_raise_only_at_safe_points(self):
        state = isolation._install_termination_handlers()
        try:
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            self.assertEqual(signal.SIGTERM, state.requested_signum)
            with self.assertRaises(isolation._TerminationRequested):
                isolation._raise_if_termination_requested()
        finally:
            isolation._restore_termination_handlers(state)

    def test_mount_manifest_detects_content_and_permission_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            private_parent = Path(directory).resolve()
            private_parent.chmod(0o700)
            root = private_parent / "source"
            root.mkdir(mode=0o755)
            file_path = root / "payload.txt"
            file_path.write_text("safe", encoding="utf-8")
            file_path.chmod(0o644)
            identity = isolation._mount_source_identity(root)
            file_path.write_text("evil", encoding="utf-8")
            with self.assertRaisesRegex(isolation.IsolationError, "changed"):
                isolation._revalidate_mount_source(identity)
            file_path.chmod(0o600)
            with self.assertRaisesRegex(isolation.IsolationError, "0755/0644"):
                isolation._mount_source_identity(root)
            file_path.chmod(0o644)
            private_parent.chmod(0o755)
            with self.assertRaisesRegex(isolation.IsolationError, "private parent"):
                isolation._mount_source_identity(root)

    def test_inspected_container_hardening_must_match_frozen_limits(self):
        runtime = runtime_record()
        host = {
            "ReadonlyRootfs": True,
            "Privileged": False,
            "NetworkMode": "private",
            "Memory": 2 * 1024 * 1024 * 1024,
            "MemorySwap": 2 * 1024 * 1024 * 1024,
            "NanoCpus": 2_000_000_000,
            "PidsLimit": isolation.CONTAINER_PIDS_LIMIT,
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "PidMode": "",
            "IpcMode": "private",
            "SecurityOpt": [
                "no-new-privileges:true",
                "seccomp=/trusted/seccomp.json",
            ],
            "MaskedPaths": [],
            "ReadonlyPaths": [],
            "Tmpfs": {
                "/workspace": isolation._tmpfs(
                    "/workspace", isolation.WORKSPACE_TMPFS_BYTES, "10001:10001"
                ).split(":", 1)[1],
                "/codex-home": isolation._tmpfs(
                    "/codex-home", isolation.STATE_TMPFS_BYTES, "10001:10001"
                ).split(":", 1)[1],
                "/tmp": isolation._tmpfs(
                    "/tmp", isolation.TMP_LIMIT_BYTES, "10001:10001"
                ).split(":", 1)[1],
            },
        }
        inspected = {
            "HostConfig": host,
            "Config": {
                "User": "10001:10001",
                "Env": ["PATH=/usr/bin:/bin", "HOME=/codex-home"],
                "Labels": {
                    isolation.RESOURCE_LABEL_KEY: isolation.RESOURCE_LABEL_VALUE,
                    isolation.RESOURCE_SCOPE_LABEL_KEY: isolation._resource_scope_label(runtime).split("=", 1)[1],
                },
            },
        }
        isolation._verify_container_hardening(
            runtime, inspected, role="agent", network="private"
        )
        valid_security_options = list(host["SecurityOpt"])
        for invalid_security_options in (
            [
                "no-new-privileges:false",
                "seccomp=/trusted/seccomp.json",
            ],
            [*valid_security_options, "seccomp=unconfined"],
            [*valid_security_options, "no-new-privileges:true"],
            [item for item in valid_security_options if not item.startswith("seccomp=")],
        ):
            with self.subTest(security_options=invalid_security_options):
                host["SecurityOpt"] = invalid_security_options
                with self.assertRaisesRegex(isolation.IsolationError, "security options"):
                    isolation._verify_container_hardening(
                        runtime, inspected, role="agent", network="private"
                    )
        host["SecurityOpt"] = valid_security_options
        host["MaskedPaths"] = ["/proc/acpi"]
        with self.assertRaisesRegex(isolation.IsolationError, "system path"):
            isolation._verify_container_hardening(
                runtime, inspected, role="agent", network="private"
            )
        host["MaskedPaths"] = []
        original_options = host["Tmpfs"]["/workspace"]
        invalid_options = (
            original_options.replace(
                "size={}".format(isolation.WORKSPACE_TMPFS_BYTES), "size=1"
            ),
            original_options.replace(
                ",size={}".format(isolation.WORKSPACE_TMPFS_BYTES), ""
            ),
            original_options.replace(",nr_inodes={}".format(isolation.SNAPSHOT_INODE_LIMIT), ""),
            original_options.replace(",mode=0700", ""),
            original_options.replace(",uid=10001", ",uid=0"),
            original_options.replace(",gid=10001", ",gid=0"),
            original_options.replace(",nosuid", ""),
            original_options.replace(",nodev", ""),
            original_options.replace(",noexec", ""),
        )
        for invalid in invalid_options:
            with self.subTest(options=invalid):
                host["Tmpfs"]["/workspace"] = invalid
                with self.assertRaisesRegex(isolation.IsolationError, "tmpfs"):
                    isolation._verify_container_hardening(
                        runtime, inspected, role="agent", network="private"
                    )
        host["Tmpfs"]["/workspace"] = original_options
        host["Privileged"] = True
        with self.assertRaisesRegex(isolation.IsolationError, "hardening"):
            isolation._verify_container_hardening(
                runtime, inspected, role="agent", network="private"
            )

    def test_proxy_security_options_are_exact(self):
        runtime = runtime_record()
        host = {
            "ReadonlyRootfs": True,
            "Privileged": False,
            "NetworkMode": "private",
            "Memory": 256 * 1024 * 1024,
            "MemorySwap": 256 * 1024 * 1024,
            "NanoCpus": 1_000_000_000,
            "PidsLimit": isolation.PROXY_PIDS_LIMIT,
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "PidMode": "",
            "IpcMode": "private",
            "SecurityOpt": ["no-new-privileges:true"],
            "Tmpfs": {
                "/tmp": isolation._tmpfs(
                    "/tmp", isolation.TMP_LIMIT_BYTES, "10001:10001"
                ).split(":", 1)[1]
            },
        }
        inspected = {
            "HostConfig": host,
            "Config": {
                "User": "10001:10001",
                "Env": ["PYTHONDONTWRITEBYTECODE=1"],
                "Labels": {
                    isolation.RESOURCE_LABEL_KEY: isolation.RESOURCE_LABEL_VALUE,
                    isolation.RESOURCE_SCOPE_LABEL_KEY: isolation._resource_scope_label(
                        runtime
                    ).split("=", 1)[1],
                },
            },
        }
        isolation._verify_container_hardening(
            runtime, inspected, role="proxy", network="private"
        )
        for accepted in (
            "no-new-privileges",
            "no-new-privileges=true",
            "no-new-privileges:true",
        ):
            host["SecurityOpt"] = [accepted]
            isolation._verify_container_hardening(
                runtime, inspected, role="proxy", network="private"
            )
        for options in (
            ["no-new-privileges:false"],
            ["no-new-privileges:true", "seccomp=unconfined"],
        ):
            with self.subTest(options=options):
                host["SecurityOpt"] = options
                with self.assertRaisesRegex(isolation.IsolationError, "security options"):
                    isolation._verify_container_hardening(
                        runtime, inspected, role="proxy", network="private"
                    )

    def test_every_docker_call_revalidates_trust_and_uses_minimal_environment(self):
        runtime = runtime_record()
        checked = []
        captured = []

        def revalidate(value):
            checked.append(value["path"])

        def capture(command, **kwargs):
            captured.append((command, kwargs["env"]))
            return isolation.Capture(0, b"ok", b"")

        with mock.patch.object(isolation, "_revalidate_identity", side_effect=revalidate), mock.patch.object(
            isolation, "_capture_process", side_effect=capture
        ):
            isolation._docker_capture(runtime, ("version",))
            isolation._docker_capture(runtime, ("info",))
        self.assertEqual(6, len(checked))
        self.assertEqual(
            {
                "DOCKER_CONFIG",
                "DOCKER_HOST",
                "HOME",
                "LANG",
                "LC_ALL",
                "PATH",
            },
            set(captured[0][1]),
        )
        self.assertEqual("/trusted/docker", captured[0][0][0])

    def test_bounded_subprocess_input_cannot_block_past_timeout(self):
        started = time.monotonic()
        capture = isolation._capture_process(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            env={"PATH": os.environ.get("PATH", "")},
            timeout=1,
            input_data=b"x" * (2 * 1024 * 1024),
        )
        self.assertTrue(capture.timed_out)
        self.assertLess(time.monotonic() - started, 4)

    def test_latched_signal_reaps_an_inflight_child_before_propagating(self):
        state = isolation._install_termination_handlers()
        managed = isolation._start_process(
            [sys.executable, "-I", "-S", "-c", "import time; time.sleep(30)"],
            env={"PATH": "/usr/bin:/bin"},
        )
        try:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            with self.assertRaises(isolation._TerminationRequested):
                isolation._finish_process(managed, 30)
            self.assertIsNotNone(managed.process.poll())
        finally:
            if managed.process.poll() is None:
                isolation._finish_process(managed, 5, terminate=True)
            isolation._restore_termination_handlers(state)


class RuntimeValidationTests(unittest.TestCase):
    def test_docker_engine_older_than_28_is_rejected_fail_closed(self):
        with self.assertRaisesRegex(isolation.IsolationError, "28 or newer"):
            isolation._required_docker_engine_version("27.5.1")
        self.assertEqual((28, 0, 0), isolation._required_docker_engine_version("28.0.0"))

    def test_validate_runtime_returns_runner_compatible_serializable_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            package = root / "package"
            (package / "scripts").mkdir(parents=True)
            (package / "SKILL.md").write_text(
                "---\nname: diagram-grill\n---\n\n# Skill\n\nA sufficiently unique discovery marker for this fixture.\n",
                encoding="utf-8",
            )
            (package / "requirements.lock").write_text("diagrams==0.25.1\n", encoding="utf-8")
            (package / "scripts" / "diagram_grill_cli.py").write_text("pass\n", encoding="utf-8")
            results = root / "results"
            state = root / "state"
            results.mkdir()
            state.mkdir(mode=0o700)
            results.chmod(0o700)
            state.chmod(0o700)

            def identity(path, kind):
                return {
                    "path": str(path),
                    "kind": kind,
                    "sha256": isolation.SECCOMP_PROFILE_SHA256
                    if kind == "regular"
                    else "d" * 64,
                }

            agent_metadata = {
                "image_id": AGENT_IMAGE,
                "repo_digests": [],
                "user": "10001:10001",
                "entrypoint": isolation.AGENT_ENTRYPOINT,
                "runtime_contract": isolation.AGENT_RUNTIME_CONTRACT,
                "labels": {},
            }
            proxy_metadata = {
                "image_id": PROXY_IMAGE,
                "repo_digests": [],
                "user": "10001:10001",
                "entrypoint": isolation.PROXY_ENTRYPOINT,
                "runtime_contract": isolation.PROXY_RUNTIME_CONTRACT,
                "labels": {},
            }
            version = isolation.Capture(
                0,
                json.dumps(
                    {"Client": {"Version": "28.0.1"}, "Server": {"Version": "28.0.1"}}
                ).encode("utf-8"),
                b"",
            )
            with mock.patch.object(isolation, "_file_identity", side_effect=identity), mock.patch.object(
                isolation, "_docker_capture", return_value=version
            ), mock.patch.object(
                isolation,
                "_image_metadata",
                side_effect=(agent_metadata, proxy_metadata),
            ), mock.patch.object(
                isolation,
                "_active_probe",
                return_value={
                    "ok": True,
                    "codex_exec_config_parsed": True,
                    "skill_discovery": {"ok": True},
                    "pip_freeze": "diagrams==0.25.1\ngraphviz==0.20.3\n",
                },
            ), mock.patch.object(
                isolation, "_boundary_probe", return_value={"ok": True}
            ), mock.patch.object(
                isolation, "_scavenge_resources", return_value={"containers": [], "networks": []}
            ):
                value = isolation.validate_runtime(
                    AGENT_IMAGE,
                    PROXY_IMAGE,
                    package,
                    results,
                    state,
                    Path("/trusted/docker"),
                    Path("/trusted/docker.sock"),
                )
        self.assertEqual(AGENT_IMAGE, value["agent_image"]["id"])
        self.assertEqual(PROXY_IMAGE, value["proxy_image"]["id"])
        self.assertEqual("codex-cli 0.149.1", value["codex_version"])
        self.assertIsInstance(value["preflight"], dict)
        self.assertIn("diagrams==", value["pip_freeze"])
        json.dumps(value)

    def test_image_metadata_rejects_wrong_immutable_label(self):
        inspected = [{
            "Id": AGENT_IMAGE,
            "RepoDigests": [],
            "Config": {
                "User": "10001:10001",
                "Entrypoint": isolation.AGENT_ENTRYPOINT,
                "Env": ["PATH=/usr/bin:/bin"],
                "Labels": {
                    "io.diagram-grill.runtime-contract": "wrong",
                    "io.diagram-grill.codex-version": "0.149.1",
                },
            },
        }]
        with mock.patch.object(
            isolation,
            "_docker_capture",
            return_value=isolation.Capture(0, json.dumps(inspected).encode(), b""),
        ):
            with self.assertRaisesRegex(isolation.IsolationError, "contract"):
                isolation._image_metadata(runtime_record(), AGENT_IMAGE, "agent")


class TurnTests(unittest.TestCase):
    def _run(
        self,
        *,
        codex_capture=None,
        agent_launch_error=False,
        credential=None,
        commit_error=None,
        raw_error=None,
        observers=None,
        real_termination_handlers=False,
        cleanup_signal_kind=None,
        repeat_cleanup_signal=False,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        run = root / "run"
        repo = run / "input" / "repo"
        skill = run / "input" / "skill-package"
        workspace = run / "snapshots" / "workspace"
        state_snapshot = root / "external-state" / "codex-home"
        for path in (repo, skill, workspace, state_snapshot, run / "raw"):
            path.mkdir(parents=True, exist_ok=True)
        runtime = runtime_record()
        runtime["roots"] = {"state": {"path": str(root / "external-state")}}
        context = {
            "run": run,
            "runtime": runtime,
            "condition": "diagram-grill",
            "model": "gpt-5.4",
            "run_id": "01-checkout--diagram-grill--r1",
            "session_id": None,
            "prompt": b"draw the architecture",
            "workspace": workspace,
            "codex_home": state_snapshot,
            "snapshot_journal": run / "snapshots" / "current.json",
            "repo": repo,
            "skill": skill,
        }
        empty = tar_bytes()
        after_workspace = tar_bytes((("diagram.py", "file", b"print('ok')\n"),))
        after_state = tar_bytes((("sessions", "dir", b""),))
        events = []
        signal_injected = False
        credential = credential or bytearray(b"sk-proj-dedicated-test-key-12345")
        codex_capture = codex_capture or isolation.Capture(
            0,
            json.dumps({"type": "thread.started", "thread_id": SESSION_ID}).encode()
            + b"\n",
            b"",
        )

        def active(*args, **kwargs):
            events.append("probe")
            return {"ok": True}

        def inject_cleanup_signal(kind):
            nonlocal signal_injected
            if signal_injected or cleanup_signal_kind != kind:
                return
            signal_injected = True
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            if repeat_cleanup_signal:
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)

        def start_proxy(*args, **kwargs):
            self.assertEqual("probe", events[-1])
            events.append("proxy")
            self.assertTrue(kwargs["credential"].startswith(b"sk-proj"))
            return "proxy-process"

        def docker_capture(runtime_value, arguments, **kwargs):
            if agent_launch_error and arguments and arguments[0] == "run":
                raise isolation.IsolationError("agent failed")
            if isolation._READ_LAST_MESSAGE_PROGRAM in arguments:
                return isolation.Capture(0, b"done", b"")
            return isolation.Capture(0, b"", b"")

        def finish(process, timeout, **kwargs):
            if process == "codex-process":
                return codex_capture
            inject_cleanup_signal("proxy_attach")
            events.append("proxy_attach")
            return isolation.Capture(137, b"", b"")

        def export(runtime_value, name, root_path, **kwargs):
            if root_path == isolation.CONTAINER_WORKSPACE:
                return after_workspace, {"sha256": "w" * 64}
            return after_state, {"sha256": "s" * 64}

        removed = []
        released = []
        restored = []
        if observers is not None:
            observers.update(
                {
                    "removed": removed,
                    "released": released,
                    "restored": restored,
                    "events": events,
                }
            )

        def remove_container(runtime_value, name):
            inject_cleanup_signal(
                "agent" if "agent" in name and "proxy" not in name else "proxy"
            )
            removed.append(("container", name))
            return {"succeeded": True}

        def remove_network(runtime_value, name):
            inject_cleanup_signal(
                "internal_network" if "internal" in name else "egress_network"
            )
            removed.append(("network", name))
            return {"succeeded": True}

        patches = [
            mock.patch.object(
                isolation,
                "_recovery_runtime",
                return_value=(run, runtime, run.parent, root / "external-state"),
            ),
            mock.patch.object(isolation, "_turn_context", return_value=context),
            mock.patch.object(
                isolation,
                "_snapshot_with_identity",
                return_value=(empty, {"path": str(workspace), "device": 1, "inode": 1, "mtime_ns": 1}),
            ),
            mock.patch.object(isolation, "_revalidate_snapshot_identity"),
            mock.patch.object(isolation, "_mount_source_identity", return_value={"path": "x"}),
            mock.patch.object(isolation, "_revalidate_mount_source"),
            mock.patch.object(isolation, "_verify_container_mount_manifest"),
            mock.patch.object(isolation, "_acquire_evaluation_lock", return_value=99),
            mock.patch.object(
                isolation,
                "_release_evaluation_lock",
                side_effect=lambda descriptor: released.append(descriptor),
            ),
            mock.patch.object(
                isolation,
                "_scavenge_resources",
                return_value={"containers": [], "networks": []},
            ),
            mock.patch.object(isolation, "_active_probe", side_effect=active),
            mock.patch.object(isolation, "_create_network", return_value={}),
            mock.patch.object(isolation, "_start_proxy", side_effect=start_proxy),
            mock.patch.object(isolation, "_agent_container_arguments", return_value=["run"]),
            mock.patch.object(isolation, "_docker_capture", side_effect=docker_capture),
            mock.patch.object(isolation, "_verify_turn_boundary", return_value={"ok": True}),
            mock.patch.object(isolation, "_import_snapshot", return_value={"ok": True}),
            mock.patch.object(isolation, "_docker_start", return_value="codex-process"),
            mock.patch.object(isolation, "_finish_process", side_effect=finish),
            mock.patch.object(isolation, "_export_snapshot", side_effect=export),
            mock.patch.object(
                isolation,
                "_commit_snapshot_pair",
                side_effect=commit_error,
                return_value={"atomic": True},
            ),
            mock.patch.object(isolation, "_remove_container", side_effect=remove_container),
            mock.patch.object(isolation, "_remove_network", side_effect=remove_network),
        ]
        if not real_termination_handlers:
            patches.extend(
                (
                    mock.patch.object(
                        isolation,
                        "_install_termination_handlers",
                        return_value={"SIGTERM": "previous"},
                    ),
                    mock.patch.object(
                        isolation,
                        "_restore_termination_handlers",
                        side_effect=lambda previous: restored.append(previous),
                    ),
                )
            )
        if raw_error is not None:
            patches.append(
                mock.patch.object(
                    isolation, "_atomic_raw_write", side_effect=raw_error
                )
            )
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            result = isolation.run_turn(
                run,
                {},
                "unused by mocked context",
                1,
                True,
                30,
                credential,
            )
        return result, credential, removed, run

    def test_success_returns_protocol_fields_persists_scanned_outputs_and_zeros_key(self):
        result, credential, removed, run = self._run()
        self.assertTrue(result["isolation"]["succeeded"])
        self.assertEqual(0, result["exit_code"])
        self.assertEqual(SESSION_ID, result["session_id"])
        self.assertEqual("raw/turn-001.jsonl", result["stdout"])
        self.assertEqual("raw/turn-001.stderr.txt", result["stderr"])
        self.assertEqual("raw/turn-001.last-message.txt", result["last_message"])
        self.assertTrue((run / result["stdout"]).is_file())
        self.assertEqual({0}, set(credential))
        self.assertEqual(["container", "container", "network", "network"], [item[0] for item in removed])

    def test_stale_resources_are_scavenged_before_journal_mismatch_refusal(self):
        credential = bytearray(b"sk-proj-dedicated-test-key-12345")
        runtime = runtime_record()
        order = []
        with mock.patch.object(
            isolation,
            "_recovery_runtime",
            return_value=(Path("/run"), runtime, Path("/results"), Path("/state")),
        ), mock.patch.object(
            isolation, "_acquire_evaluation_lock", return_value=41
        ), mock.patch.object(
            isolation,
            "_install_termination_handlers",
            return_value={"mocked": True},
        ), mock.patch.object(
            isolation, "_restore_termination_handlers"
        ), mock.patch.object(
            isolation,
            "_scavenge_resources",
            side_effect=lambda _runtime: order.append("scavenge") or {},
        ), mock.patch.object(
            isolation,
            "_turn_context",
            side_effect=isolation.IsolationError("snapshot journal mismatch"),
        ), mock.patch.object(
            isolation,
            "_release_evaluation_lock",
            side_effect=lambda _descriptor: order.append("release"),
        ):
            with self.assertRaisesRegex(isolation.IsolationError, "journal"):
                isolation.run_turn(
                    Path("/run"), {}, "prompt", 1, True, 30, credential
                )
        self.assertEqual(["scavenge", "release"], order)
        self.assertEqual({0}, set(credential))

    def test_timeout_contains_survivors_forces_teardown_and_retains_old_snapshots(self):
        capture = isolation.Capture(-9, b"partial", b"", timed_out=True)
        with mock.patch.object(isolation, "_commit_snapshot_pair") as commit:
            result, credential, removed, _ = self._run(codex_capture=capture)
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["isolation"]["succeeded"])
        self.assertEqual({0}, set(credential))
        commit.assert_not_called()
        self.assertEqual("container", removed[0][0])
        self.assertIn("agent", removed[0][1])
        self.assertIn("proxy", removed[1][1])

    def test_nonzero_quiescent_exit_commits_bounded_partial_snapshots_but_fails_run(self):
        capture = isolation.Capture(
            1,
            json.dumps({"type": "thread.started", "thread_id": SESSION_ID}).encode() + b"\n",
            b"failed",
        )
        result, credential, _, _ = self._run(codex_capture=capture)
        self.assertEqual(1, result["exit_code"])
        self.assertEqual("Codex execution failed", result["launch_error"])
        self.assertIsNotNone(result["isolation"]["snapshot_commit"])
        self.assertFalse(result["isolation"]["succeeded"])
        self.assertEqual({0}, set(credential))

    def test_termination_during_snapshot_commit_unwinds_after_cleanup(self):
        credential = bytearray(b"sk-proj-dedicated-test-key-12345")
        observers = {}
        with self.assertRaises(isolation._TerminationRequested):
            self._run(
                credential=credential,
                commit_error=isolation._TerminationRequested("signal"),
                observers=observers,
            )
        self.assertEqual({0}, set(credential))
        self.assertEqual(4, len(observers["removed"]))
        self.assertEqual([99], observers["released"])
        self.assertEqual([{"SIGTERM": "previous"}], observers["restored"])

    def test_termination_during_raw_persistence_unwinds_after_cleanup(self):
        credential = bytearray(b"sk-proj-dedicated-test-key-12345")
        observers = {}
        with self.assertRaises(isolation._TerminationRequested):
            self._run(
                credential=credential,
                raw_error=isolation._TerminationRequested("signal"),
                observers=observers,
            )
        self.assertEqual({0}, set(credential))
        self.assertEqual(4, len(observers["removed"]))
        self.assertEqual([99], observers["released"])
        self.assertEqual([{"SIGTERM": "previous"}], observers["restored"])

    def test_repeated_signals_during_each_cleanup_action_are_deferred(self):
        for cleanup_kind in (
            "agent",
            "proxy",
            "proxy_attach",
            "internal_network",
            "egress_network",
        ):
            with self.subTest(cleanup_kind=cleanup_kind):
                previous = {
                    signum: signal.getsignal(signum)
                    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
                }
                credential = bytearray(b"sk-proj-dedicated-test-key-12345")
                observers = {}
                with self.assertRaises(isolation._TerminationRequested):
                    self._run(
                        credential=credential,
                        observers=observers,
                        real_termination_handlers=True,
                        cleanup_signal_kind=cleanup_kind,
                        repeat_cleanup_signal=True,
                    )
                self.assertEqual({0}, set(credential))
                self.assertEqual(4, len(observers["removed"]))
                self.assertIn("proxy_attach", observers["events"])
                self.assertEqual([99], observers["released"])
                self.assertIsNone(isolation._TERMINATION_STATE)
                for signum, handler in previous.items():
                    self.assertIs(signal.getsignal(signum), handler)

    def test_signal_at_cleanup_deferral_entry_cannot_skip_cleanup(self):
        for injection_call in (1, 2):
            with self.subTest(injection_call=injection_call):
                credential = bytearray(b"sk-proj-dedicated-test-key-12345")
                observers = {}
                calls = 0
                original = isolation._defer_termination

                def defer_with_signal_at_entry():
                    nonlocal calls
                    calls += 1
                    if calls == injection_call:
                        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                    return original()

                with mock.patch.object(
                    isolation,
                    "_defer_termination",
                    side_effect=defer_with_signal_at_entry,
                ):
                    with self.assertRaises(isolation._TerminationRequested):
                        self._run(
                            credential=credential,
                            observers=observers,
                            real_termination_handlers=True,
                        )
                self.assertEqual({0}, set(credential))
                self.assertEqual(4, len(observers["removed"]))
                self.assertIn("proxy_attach", observers["events"])
                self.assertEqual([99], observers["released"])
                self.assertIsNone(isolation._TERMINATION_STATE)

    def test_agent_launch_error_still_force_tears_down_all_named_resources(self):
        result, credential, removed, _ = self._run(agent_launch_error=True)
        self.assertRegex(result["launch_error"], "agent launch")
        self.assertFalse(result["isolation"]["succeeded"])
        self.assertEqual(4, len(removed))
        self.assertEqual({0}, set(credential))

    def test_output_secret_is_never_persisted(self):
        secret = b"sk-proj-dedicated-test-key-12345"
        capture = isolation.Capture(
            0,
            json.dumps({"type": "thread.started", "thread_id": SESSION_ID}).encode()
            + b"\n"
            + secret,
            b"",
        )
        result, credential, _, run = self._run(codex_capture=capture)
        self.assertIsNone(result["stdout"])
        self.assertIn("credential", result["transport_violation"])
        self.assertFalse(any((run / "raw").glob("turn-001*")))
        self.assertEqual({0}, set(credential))

    def test_turn_limit_fails_closed_and_zeroes_key(self):
        credential = bytearray(b"sk-proj-dedicated-test-key-12345")
        with self.assertRaisesRegex(isolation.IsolationError, "turn"):
            isolation.run_turn(Path("/does/not/matter"), {}, "prompt", 13, True, 30, credential)
        self.assertEqual({0}, set(credential))


if __name__ == "__main__":
    unittest.main()
