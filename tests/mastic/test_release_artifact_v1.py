import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RELEASE_STAGER = ROOT / "scripts" / "stage-release-artifacts.zsh"
RELEASE_PUBLISHER = ROOT / "scripts" / "publish-release.zsh"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
HOST_FIXTURE_WORKFLOW = ROOT / ".github" / "workflows" / "bootstrap-artifact.yml"
_SUBPROCESS_TIMEOUT = 30


class ReleaseArtifactV1Tests(unittest.TestCase):
    _VERSION = "0.1.0"
    _TAG = f"v{_VERSION}"
    _COMMIT = "a" * 40
    _REPOSITORY = "nisavid/mastic"

    def test_release_stager_emits_only_mastic_distributions_and_checksums(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wheel = root / "mastic-0.1.0-py3-none-any.whl"
            source = root / "mastic-0.1.0.tar.gz"
            output = root / "release"
            wheel.write_bytes(b"mastic wheel")
            source.write_bytes(b"mastic source")

            completed = subprocess.run(
                ["zsh", str(RELEASE_STAGER), str(output), str(wheel), str(source)],
                capture_output=True,
                text=True,
                check=False,
                timeout=_SUBPROCESS_TIMEOUT,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    wheel.name,
                    f"{wheel.name}.sha256",
                    source.name,
                    f"{source.name}.sha256",
                },
            )
            for artifact in (wheel, source):
                staged = output / artifact.name
                self.assertEqual(staged.read_bytes(), artifact.read_bytes())
                self.assertEqual(
                    (output / f"{artifact.name}.sha256").read_text(),
                    f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  "
                    f"{artifact.name}\n",
                )

    def test_release_stager_rejects_non_mastic_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wheel = root / "mastic-0.1.0-py3-none-any.whl"
            dependency = root / "hindsight-api-0.8.4-macos-arm64.tar.gz"
            output = root / "release"
            wheel.write_bytes(b"mastic wheel")
            dependency.write_bytes(b"third-party dependency")

            completed = subprocess.run(
                [
                    "zsh",
                    str(RELEASE_STAGER),
                    str(output),
                    str(wheel),
                    str(dependency),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=_SUBPROCESS_TIMEOUT,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("expected a MASTIC source distribution", completed.stderr)
            self.assertFalse(output.exists())

    def test_release_stager_rejects_symlinked_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wheel = root / "mastic-0.1.0-py3-none-any.whl"
            wheel.write_bytes(b"mastic wheel")
            linked_wheel = root / "linked-wheel"
            linked_wheel.symlink_to(wheel)
            source = root / "mastic-0.1.0.tar.gz"
            source.write_bytes(b"mastic source")

            completed = subprocess.run(
                [
                    "zsh",
                    str(RELEASE_STAGER),
                    str(root / "release"),
                    str(linked_wheel),
                    str(source),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=_SUBPROCESS_TIMEOUT,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("expected a regular MASTIC wheel", completed.stderr)

    def test_tag_release_and_host_fixture_have_disjoint_publication_boundaries(
        self,
    ) -> None:
        release = RELEASE_WORKFLOW.read_text()
        host_fixture = HOST_FIXTURE_WORKFLOW.read_text()

        self.assertIn('tags:\n      - "v*"', release)
        self.assertIn("path: dist/release/", release)
        self.assertIn('subject-path: "release/mastic-*"', release)
        self.assertIn("zsh scripts/publish-release.zsh", release)
        self.assertIn(
            'tag_commit=$(git rev-parse "${GITHUB_REF_NAME}^{commit}")',
            release,
        )
        self.assertIn('"${{ needs.package.outputs.tag-commit }}"', release)
        self.assertIn("timeout-minutes: 30", release)
        self.assertIn("timeout-minutes: 15", release)
        self.assertNotIn("dist/*", release)
        for forbidden in (
            "bootstrap-mastic",
            "bootstrap-closure",
            "host-test-fixture",
            "hindsight-api",
            "codex-aarch64",
        ):
            self.assertNotIn(forbidden, release)

        self.assertNotIn('tags:\n      - "v*"', host_fixture)
        self.assertNotIn("gh release create", host_fixture)
        self.assertIn("mastic-host-test-fixture-${{ github.sha }}", host_fixture)
        self.assertIn("retention-days: 14", host_fixture)

    def test_release_publisher_publishes_only_the_verified_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = self._stage_release(root)
            tools, log = self._fake_release_tools(root)

            completed = self._publish(release, tools, log)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            commands = log.read_text().splitlines()
            self.assertEqual(len(commands), 3)
            expected_names = {
                f"mastic-{self._VERSION}-py3-none-any.whl",
                f"mastic-{self._VERSION}-py3-none-any.whl.sha256",
                f"mastic-{self._VERSION}.tar.gz",
                f"mastic-{self._VERSION}.tar.gz.sha256",
            }
            create_arguments = commands[0].split("\t")
            self.assertEqual(create_arguments[:3], ["release", "create", self._TAG])
            self.assertEqual(
                {Path(argument).name for argument in create_arguments[3:7]},
                expected_names,
            )
            self.assertEqual(
                create_arguments[7:],
                [
                    "--repo",
                    self._REPOSITORY,
                    "--draft",
                    "--verify-tag",
                    "--generate-notes",
                ],
            )
            self.assertEqual(
                commands[1].split("\t")[:3],
                ["release", "view", self._TAG],
            )
            self.assertEqual(
                commands[2].split("\t"),
                [
                    "release",
                    "edit",
                    self._TAG,
                    "--repo",
                    self._REPOSITORY,
                    "--draft=false",
                ],
            )

    def test_release_publisher_rejects_a_host_fixture_before_github(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = self._stage_release(root)
            tools, log = self._fake_release_tools(root)
            (release / "mastic-host-test-fixture-0.1.0-macos-arm64.tar.gz").write_bytes(
                b"fixture"
            )

            completed = self._publish(release, tools, log)

            self.assertEqual(completed.returncode, 2)
            self.assertIn("exactly four MASTIC files", completed.stderr)
            self.assertFalse(log.exists())

    def test_release_publisher_rejects_a_hidden_file_before_github(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = self._stage_release(root)
            tools, log = self._fake_release_tools(root)
            (release / ".undeclared").write_bytes(b"not a MASTIC release artifact")

            completed = self._publish(release, tools, log)

            self.assertEqual(completed.returncode, 2)
            self.assertIn("exactly four MASTIC files", completed.stderr)
            self.assertFalse(log.exists())

    def test_release_publisher_rejects_a_moved_remote_tag_before_github(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = self._stage_release(root)
            tools, log = self._fake_release_tools(root, commit="b" * 40)

            completed = self._publish(release, tools, log)

            self.assertEqual(completed.returncode, 2)
            self.assertIn("remote tag target changed", completed.stderr)
            self.assertFalse(log.exists())

    def test_release_publisher_resumes_an_existing_draft(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = self._stage_release(root)
            tools, log = self._fake_release_tools(root)

            completed = self._publish(release, tools, log, release_state="true")

            self.assertEqual(completed.returncode, 0, completed.stderr)
            commands = [line.split("\t") for line in log.read_text().splitlines()]
            mutations = [
                arguments
                for arguments in commands
                if arguments[:2] != ["release", "view"]
            ]
            self.assertEqual(mutations[0][:3], ["release", "upload", self._TAG])
            self.assertEqual(
                {Path(argument).name for argument in mutations[0][3:7]},
                {
                    f"mastic-{self._VERSION}-py3-none-any.whl",
                    f"mastic-{self._VERSION}-py3-none-any.whl.sha256",
                    f"mastic-{self._VERSION}.tar.gz",
                    f"mastic-{self._VERSION}.tar.gz.sha256",
                },
            )
            self.assertEqual(
                mutations[0][7:],
                ["--repo", self._REPOSITORY, "--clobber"],
            )
            self.assertEqual(
                mutations[1],
                [
                    "release",
                    "edit",
                    self._TAG,
                    "--repo",
                    self._REPOSITORY,
                    "--draft=false",
                ],
            )

    def test_release_publisher_rejects_an_existing_published_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = self._stage_release(root)
            tools, log = self._fake_release_tools(root)

            completed = self._publish(release, tools, log, release_state="false")

            self.assertEqual(completed.returncode, 2)
            self.assertIn("already exists and is not a draft", completed.stderr)
            commands = [line.split("\t") for line in log.read_text().splitlines()]
            self.assertTrue(commands)
            self.assertFalse(
                any(
                    arguments[1] in {"create", "upload", "edit"}
                    for arguments in commands
                )
            )

    def test_release_publisher_resolves_a_lightweight_tag(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = self._stage_release(root)
            tools, log = self._fake_release_tools(root)

            completed = self._publish(release, tools, log, tag_kind="lightweight")

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                (root / "git.log").read_text().splitlines(),
                [
                    f"refs/tags/{self._TAG}^{{}}",
                    f"refs/tags/{self._TAG}",
                    f"refs/tags/{self._TAG}^{{}}",
                    f"refs/tags/{self._TAG}",
                ],
            )

    def test_release_publisher_stops_if_tag_moves_after_draft_creation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = self._stage_release(root)
            tools, log = self._fake_release_tools(root)

            completed = self._publish(release, tools, log, second_commit="b" * 40)

            self.assertEqual(completed.returncode, 2)
            self.assertIn(
                "remote tag target changed before publication",
                completed.stderr,
            )
            commands = [line.split("\t") for line in log.read_text().splitlines()]
            self.assertTrue(
                any(arguments[:2] == ["release", "create"] for arguments in commands)
            )
            self.assertFalse(
                any(arguments[:2] == ["release", "edit"] for arguments in commands)
            )

    def test_release_publisher_rejects_a_symlinked_release_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = self._stage_release(root)
            linked_release = root / "linked-release"
            linked_release.symlink_to(release, target_is_directory=True)
            tools, log = self._fake_release_tools(root)

            completed = self._publish(linked_release, tools, log)

            self.assertEqual(completed.returncode, 2)
            self.assertIn("release directory does not exist", completed.stderr)
            self.assertFalse(log.exists())

    def _stage_release(self, root: Path) -> Path:
        wheel = root / f"mastic-{self._VERSION}-py3-none-any.whl"
        source = root / f"mastic-{self._VERSION}.tar.gz"
        release = root / "release"
        wheel.write_bytes(b"mastic wheel")
        source.write_bytes(b"mastic source")
        subprocess.run(
            ["zsh", str(RELEASE_STAGER), str(release), str(wheel), str(source)],
            check=True,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        return release

    def _fake_release_tools(
        self,
        root: Path,
        *,
        commit: str | None = None,
    ) -> tuple[Path, Path]:
        tools = root / "tools"
        tools.mkdir()
        log = root / "gh.log"
        git = tools / "git"
        git.write_text(
            "#!/bin/zsh\n"
            "ref=${@[-1]}\n"
            'print -r -- "$ref" >>$GIT_CALL_LOG\n'
            "if [[ ${FAKE_TAG_KIND:-annotated} == lightweight "
            "&& $ref == *'^{}' ]]; then\n"
            "  exit 0\n"
            "fi\n"
            "count=0\n"
            "[[ -f $GIT_SUCCESS_LOG ]] && read -r count <$GIT_SUCCESS_LOG\n"
            "(( count += 1 ))\n"
            'print -r -- "$count" >$GIT_SUCCESS_LOG\n'
            f"target='{commit or self._COMMIT}'\n"
            "[[ $count -gt 1 && -n ${FAKE_SECOND_COMMIT:-} ]] "
            "&& target=$FAKE_SECOND_COMMIT\n"
            'print -r -- "$target\t$ref"\n'
        )
        git.chmod(0o755)
        gh = tools / "gh"
        gh.write_text(
            "#!/bin/zsh\n"
            "if [[ $1 == release && $2 == view && $* == *'.isDraft'* ]]; then\n"
            "  [[ -n ${FAKE_RELEASE_STATE:-} ]] || exit 1\n"
            '  print -r -- "$FAKE_RELEASE_STATE"\n'
            '  print -r -- "${(j:\t:)@}" >>$COMMAND_LOG\n'
            "  exit 0\n"
            "fi\n"
            "if [[ $1 == release && $2 == view && $* == *'.assets[].name'* ]]; then\n"
            f"  print -r -- 'mastic-{self._VERSION}-py3-none-any.whl'\n"
            f"  print -r -- 'mastic-{self._VERSION}-py3-none-any.whl.sha256'\n"
            f"  print -r -- 'mastic-{self._VERSION}.tar.gz'\n"
            f"  print -r -- 'mastic-{self._VERSION}.tar.gz.sha256'\n"
            "fi\n"
            'print -r -- "${(j:\t:)@}" >>$COMMAND_LOG\n'
        )
        gh.chmod(0o755)
        return tools, log

    def _publish(
        self,
        release: Path,
        tools: Path,
        log: Path,
        *,
        release_state: str | None = None,
        tag_kind: str | None = None,
        second_commit: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{tools}:{environment['PATH']}"
        environment["COMMAND_LOG"] = str(log)
        environment["GIT_CALL_LOG"] = str(tools.parent / "git.log")
        environment["GIT_SUCCESS_LOG"] = str(tools.parent / "git-success.log")
        environment["ZDOTDIR"] = str(tools.parent)
        if release_state is not None:
            environment["FAKE_RELEASE_STATE"] = release_state
        if tag_kind is not None:
            environment["FAKE_TAG_KIND"] = tag_kind
        if second_commit is not None:
            environment["FAKE_SECOND_COMMIT"] = second_commit
        return subprocess.run(
            [
                "zsh",
                str(RELEASE_PUBLISHER),
                self._TAG,
                self._COMMIT,
                self._REPOSITORY,
                str(release),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=_SUBPROCESS_TIMEOUT,
        )


if __name__ == "__main__":
    unittest.main()
