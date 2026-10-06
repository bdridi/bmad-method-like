import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS = REPO_ROOT / "skills"
SHA_A = "a" * 40
SHA_B = "b" * 40


def load_init():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("agf_init", REPO_ROOT / "agf" / "init.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["agf_init"] = module
    spec.loader.exec_module(module)
    return module


init = load_init()


def write(path: Path, content: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def commit_all(repo: Path) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "wip")


TEST_SKILLS = ("bmad", "bmod-core-tools", "bmod-extra", "extra-skill")


class VersionTest(unittest.TestCase):
    def test_a_git_clone_reports_its_commit_and_exact_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            write(repo / "a")
            commit_all(repo)
            untagged = init.determine_version(repo)
            git(repo, "tag", "v1.2.3")
            tagged = init.determine_version(repo)
        self.assertEqual(untagged.sha, tagged.sha)
        self.assertEqual(len(tagged.sha), 40)
        self.assertIsNone(untagged.ref)
        self.assertEqual(tagged.ref, "v1.2.3")

    def test_an_npx_unpack_reports_the_resolved_commit_and_requested_ref(self):
        for spec, ref in (("github:owner/repo#v1.0.0", "v1.0.0"), ("github:owner/repo", None)):
            with self.subTest(spec=spec), tempfile.TemporaryDirectory() as temp:
                cache = Path(temp)
                root = cache / "node_modules" / "agf"
                root.mkdir(parents=True)
                resolved = f"git+ssh://git@github.com/owner/repo.git#{SHA_A}"
                write(
                    cache / "node_modules" / ".package-lock.json",
                    json.dumps({"packages": {"node_modules/agf": {"resolved": resolved}}}),
                )
                write(cache / "package.json", json.dumps({"dependencies": {"agf": spec}}))
                version = init.determine_version(root)
                self.assertEqual(version, init.Version("owner/repo", ref, SHA_A))

    def test_a_directory_with_no_git_and_no_npx_metadata_is_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "node_modules" / "agf"
            root.mkdir(parents=True)
            with self.assertRaises(init.InitError):
                init.determine_version(root)

    def test_github_slug_accepts_the_common_url_forms_and_nothing_else(self):
        for url in (
            "https://github.com/owner/repo.git",
            "git@github.com:owner/repo.git",
            "git+https://github.com/owner/repo.git#abc",
            "https://github.com/owner/repo",
        ):
            self.assertEqual(init.github_slug(url), "owner/repo", url)
        self.assertIsNone(init.github_slug("file:///tmp/repo"))
        self.assertIsNone(init.github_slug(None))


class HarnessVersionTest(unittest.TestCase):
    def test_the_version_is_written_and_rewritten_without_touching_other_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            first = init.Version("owner/repo", "v1.0.0", SHA_A)
            self.assertTrue(init.write_version(project, first))
            self.assertFalse(init.write_version(project, first))

            path = project / init.CONFIG_FILE
            path.write_text(
                '[core]\nx = "1"\n\n' + path.read_text(encoding="utf-8") + '\n[other]\ny = "2"\n', encoding="utf-8"
            )
            second = init.Version("owner/repo", None, SHA_B)
            self.assertTrue(init.write_version(project, second))
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["core"], {"x": "1"})
            self.assertEqual(data["other"], {"y": "2"})
            self.assertEqual(data["harness"], {"source": "owner/repo", "sha": SHA_B})

    def test_the_version_is_appended_to_a_config_that_has_none(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            write(project / init.CONFIG_FILE, '[core]\nx = "1"')
            init.write_version(project, init.Version(None, "v1.0.0", SHA_A))
            data = tomllib.loads((project / init.CONFIG_FILE).read_text(encoding="utf-8"))
        self.assertEqual(data["core"], {"x": "1"})
        self.assertEqual(data["harness"]["ref"], "v1.0.0")


class GitignoreTest(unittest.TestCase):
    def test_lines_are_added_once_and_equivalent_spellings_count_as_present(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            write(project / ".gitignore", "node_modules/\n/_bmad/render")
            added = init.ensure_gitignore(project)
            self.assertEqual(added, ["*.user.toml", "skills-lock.json"])
            self.assertEqual(init.ensure_gitignore(project), [])
            lines = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["node_modules/", "/_bmad/render", "*.user.toml", "skills-lock.json"])

    def test_a_missing_gitignore_is_created(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            init.ensure_gitignore(project)
            lines = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, list(init.GITIGNORE_LINES))


class ShippedConfigTest(unittest.TestCase):
    def test_every_skill_of_the_harness_exists(self):
        self.assertIn("bmad", init.SKILLS)
        for skill in init.SKILLS:
            self.assertTrue((SKILLS / skill / "SKILL.md").is_file(), skill)

    def test_every_shipped_config_file_is_valid_toml(self):
        files = sorted(init.CONFIG_SOURCE.glob("*.toml"))
        self.assertTrue(files)
        for file in files:
            tomllib.loads(file.read_text(encoding="utf-8"))


class AnswersTest(unittest.TestCase):
    def test_resolve_answers_takes_the_default_and_fails_without_one(self):
        questions = [
            {"module": "m", "key": "a", "prompt": "A?", "default": "da", "scope": "team"},
            {"module": "m", "key": "b", "prompt": "B?", "default": "db", "scope": "user"},
        ]
        self.assertEqual(init.resolve_answers(questions), {"m": {"a": "da", "b": "db"}})
        questions.append({"module": "m", "key": "c", "prompt": "C?", "default": "", "scope": "team"})
        with self.assertRaisesRegex(init.InitError, r"m\.c"):
            init.resolve_answers(questions)

    def test_answers_toml_round_trips_awkward_values(self):
        answers = {"m": {"a.b": 'say "hi"\\n', "c": "ünï"}}
        self.assertEqual(tomllib.loads(init.answers_toml(answers))["modules"], answers)


class ConfigSyncTest(unittest.TestCase):
    def test_seeding_adds_only_the_files_the_team_does_not_have(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write(root / "source" / "a.toml", "[agent]\nx = 1\n")
            write(root / "source" / "b.toml", "[agent]\nx = 2\n")
            write(root / "project" / init.CONFIG_DIR / "a.toml", "[agent]\nx = 99\n")
            added = init.seed_config(root / "project", root / "source")
            self.assertEqual(added, ["b.toml"])
            self.assertIn("99", (root / "project" / init.CONFIG_DIR / "a.toml").read_text(encoding="utf-8"))

    def test_syncing_replaces_copies_renames_custom_and_leaves_other_files(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            write(project / init.CONFIG_DIR / "custom.toml", '[harness]\nsha = "a"\n')
            write(project / init.CONFIG_DIR / "bmad-agent-pm.toml", "[agent]\nnew = 1\n")
            write(project / init.CUSTOM_DIR / "bmad-agent-pm.toml", "[agent]\nedited = 1\n")
            write(project / init.CUSTOM_DIR / "config.user.toml", "[mine]\n")
            self.assertEqual(init.sync_custom(project), ["bmad-agent-pm.toml", "config.toml"])
            self.assertEqual(init.sync_custom(project), [])
            custom = project / init.CUSTOM_DIR
            self.assertEqual((custom / "bmad-agent-pm.toml").read_text(encoding="utf-8"), "[agent]\nnew = 1\n")
            self.assertEqual((custom / "config.toml").read_text(encoding="utf-8"), '[harness]\nsha = "a"\n')
            self.assertEqual((custom / "config.user.toml").read_text(encoding="utf-8"), "[mine]\n")


class RunInitTest(unittest.TestCase):
    """init against real setup.py, with the skills CLI replaced by a copy from a prepared skills folder."""

    @classmethod
    def setUpClass(cls):
        cls._source = tempfile.TemporaryDirectory()
        cls.source = Path(cls._source.name)
        for name in ("bmad", "bmod-core-tools"):
            shutil.copytree(SKILLS / name, cls.source / name)
        write(cls.source / "extra-skill" / "SKILL.md", "---\nname: extra-skill\n---\n")
        write(cls.source / "extra-skill" / "bmod.toml", '[skill]\nbmod = "bmod-extra"\nsource = "file:skills"\n')
        write(cls.source / "bmod-extra" / "SKILL.md", "---\nname: bmod-extra\n---\n")
        write(
            cls.source / "bmod-extra" / "bmod.toml",
            '[bmod]\ncode = "extra"\nversion = "1.0.0"\nupdate_source = "file:skills"\nskills = ["bmod-extra", "extra-skill"]\n\n'
            '[[bmod.config_questions]]\nkey = "greeting"\nprompt = "Greeting?"\ndefault = "hello"\n\n'
            '[[bmod.config_questions]]\nkey = "site"\nprompt = "Site?"\ndefault = "example"\n\n'
            '[[bmod.config_questions]]\nkey = "nick"\nprompt = "Nick?"\ndefault = "me"\nscope = "user"\n\n'
            '[skill]\nbmod = "bmod-extra"\nsource = "file:skills"\n',
        )

    @classmethod
    def tearDownClass(cls):
        cls._source.cleanup()

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.temp = Path(self._temp.name)
        self.calls: list[list[str]] = []
        self.project = self.new_repo("project")
        self.config = self.temp / "config"
        write(self.config / "bmad-agent-pm.toml", '[agent]\npersistent_facts = ["fact"]\n')
        self.v1 = init.Version("owner/repo", "v1.0.0", SHA_A)
        self.v2 = init.Version("owner/repo", "v1.1.0", SHA_B)

    def new_repo(self, name: str) -> Path:
        repo = self.temp / name
        repo.mkdir()
        git(repo, "init", "-q")
        return repo

    def installer(self, project: Path, _checkout: Path, agent: str, skills: list[str]) -> None:
        self.calls.append(list(skills))
        for skill in (s for s in skills if (self.source / s).is_dir()):
            shutil.copytree(self.source / skill, project / f".{agent}" / "skills" / skill, dirs_exist_ok=True)

    def run_init(self, project: Path, version, skills=TEST_SKILLS):
        return init.run_init(project, "tool", version, self.installer, skills, self.config)

    def state(self, project: Path) -> dict[str, bytes]:
        return {
            p.relative_to(project).as_posix(): p.read_bytes()
            for p in sorted(project.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(project).parts
        }

    def test_a_repo_is_prepared_from_the_harness(self):
        report = self.run_init(self.project, self.v1)

        self.assertEqual(self.calls, [list(TEST_SKILLS)])
        config = tomllib.loads((self.project / "_bmad" / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["modules"]["extra"]["greeting"], "hello")
        self.assertEqual(config["modules"]["extra"]["site"], "example")
        user = tomllib.loads((self.project / "_bmad" / "custom" / "config.user.toml").read_text(encoding="utf-8"))
        self.assertEqual(user["modules"]["extra"]["nick"], "me")
        self.assertEqual(sorted(report.answers_added), ["extra.greeting", "extra.nick", "extra.site"])
        self.assertEqual(report.config_seeded, ["bmad-agent-pm.toml"])
        self.assertTrue((self.project / "_agf" / "logs").is_dir())
        team = tomllib.loads((self.project / "_agf" / "custom.toml").read_text(encoding="utf-8"))
        self.assertEqual(team["harness"]["sha"], SHA_A)
        self.assertEqual(sorted(report.custom_synced), ["bmad-agent-pm.toml", "config.toml"])
        copied = tomllib.loads((self.project / "_bmad" / "custom" / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(copied["harness"]["ref"], "v1.0.0")
        ignored = (self.project / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertTrue({"_bmad/render/", "*.user.toml"} <= set(ignored))
        self.assertTrue(report.check["current"], report.check)

    def test_running_it_again_changes_nothing(self):
        self.run_init(self.project, self.v1)
        commit_all(self.project)
        before = self.state(self.project)

        report = self.run_init(self.project, self.v1)

        self.assertEqual(self.state(self.project), before)
        self.assertEqual(git(self.project, "status", "--porcelain"), "")
        self.assertEqual((report.config_seeded, report.custom_synced, report.version_written), ([], [], False))

    def test_a_new_version_reinstalls_and_moves_the_recorded_version(self):
        self.run_init(self.project, self.v1)
        commit_all(self.project)
        self.calls.clear()

        self.run_init(self.project, self.v2)

        self.assertEqual(self.calls, [list(TEST_SKILLS)])
        changed = [line.split()[-1] for line in git(self.project, "status", "--porcelain").splitlines()]
        self.assertEqual(sorted(changed), ["_agf/custom.toml", "_bmad/custom/config.toml"])

    def test_team_config_wins_over_the_shipped_defaults_and_over_edits_in_bmad_custom(self):
        self.run_init(self.project, self.v1)
        write(self.project / "_agf" / "bmad-agent-pm.toml", '[agent]\npersistent_facts = ["team"]\n')
        write(self.project / "_bmad" / "custom" / "bmad-agent-pm.toml", '[agent]\npersistent_facts = ["local"]\n')
        write(self.config / "bmad-agent-pm.toml", '[agent]\npersistent_facts = ["new default"]\n')

        report = self.run_init(self.project, self.v1)

        self.assertEqual(report.config_seeded, [])
        copied = (self.project / "_bmad" / "custom" / "bmad-agent-pm.toml").read_text(encoding="utf-8")
        self.assertIn("team", copied)
        self.assertNotIn("local", copied)

    def test_running_without_a_ref_warns_that_it_is_on_the_default_branch(self):
        report = self.run_init(self.project, init.Version(None, None, SHA_A))
        self.assertTrue(any("default branch" in warning for warning in report.warnings))

    def test_a_skill_the_installer_did_not_provide_is_an_error(self):
        with self.assertRaisesRegex(init.InitError, "no-such-skill"):
            self.run_init(self.project, self.v1, skills=("bmad", "bmod-core-tools", "no-such-skill"))


if __name__ == "__main__":
    unittest.main()
