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
    spec = importlib.util.spec_from_file_location("lke_init", REPO_ROOT / "init" / "init.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["lke_init"] = module
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


def profile_with(name: str = "demo", **overrides) -> "init.Profile":
    values = {
        "name": name,
        "agent": "tool",
        "skills": ("bmad", "bmod-core-tools", "bmod-extra", "extra-skill"),
        "answers": {},
        "custom": {},
    }
    values.update(overrides)
    return init.Profile(**values)


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
                root = cache / "node_modules" / "lke"
                root.mkdir(parents=True)
                resolved = f"git+ssh://git@github.com/owner/repo.git#{SHA_A}"
                write(
                    cache / "node_modules" / ".package-lock.json",
                    json.dumps({"packages": {"node_modules/lke": {"resolved": resolved}}}),
                )
                write(cache / "package.json", json.dumps({"dependencies": {"lke": spec}}))
                version = init.determine_version(root)
                self.assertEqual(version, init.Version("owner/repo", ref, SHA_A))

    def test_a_directory_with_no_git_and_no_npx_metadata_is_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "node_modules" / "lke"
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

    def test_compare_versions(self):
        v = lambda ref, sha: init.Version(None, ref, sha)  # noqa: E731
        cases = (
            (v("v1.0.0", SHA_A), v("v1.0.0", SHA_A), "same"),
            (v(None, SHA_A), v("v9.0.0", SHA_A), "same"),
            (v("v1.10.0", SHA_A), v("v1.9.0", SHA_B), "newer"),
            (v("v1.0.0", SHA_A), v("v1.0.1", SHA_B), "older"),
            (v("v1.0.0", SHA_A), v("v1.0.0", SHA_B), "changed"),
            (v(SHA_A, SHA_A), v("v1.0.0", SHA_B), "changed"),
            (v(None, SHA_A), v(None, SHA_B), "changed"),
        )
        for current, pinned, expected in cases:
            self.assertEqual(init.compare_versions(current, pinned), expected, (current, pinned))


class PinTest(unittest.TestCase):
    def test_the_pin_is_written_read_back_and_rewritten_without_touching_other_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.assertIsNone(init.read_pin(project))
            first = init.Version("owner/repo", "v1.0.0", SHA_A)
            self.assertTrue(init.write_pin(project, first))
            self.assertEqual(init.read_pin(project), first)
            self.assertFalse(init.write_pin(project, first))

            path = project / init.PIN_FILE
            path.write_text(
                '[core]\nx = "1"\n\n' + path.read_text(encoding="utf-8") + '\n[other]\ny = "2"\n', encoding="utf-8"
            )
            second = init.Version("owner/repo", None, SHA_B)
            self.assertTrue(init.write_pin(project, second))
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["core"], {"x": "1"})
            self.assertEqual(data["other"], {"y": "2"})
            self.assertEqual(data["harness"], {"source": "owner/repo", "sha": SHA_B})

    def test_a_pin_is_appended_to_a_config_that_has_none(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            write(project / init.PIN_FILE, '[core]\nx = "1"')
            init.write_pin(project, init.Version(None, "v1.0.0", SHA_A))
            data = tomllib.loads((project / init.PIN_FILE).read_text(encoding="utf-8"))
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


class ProfileTest(unittest.TestCase):
    def test_every_shipped_profile_loads_and_lists_skills_that_exist(self):
        profiles = sorted(p.name for p in (REPO_ROOT / "profiles").iterdir() if p.is_dir())
        self.assertGreaterEqual(len(profiles), 2)
        for name in profiles:
            profile = init.load_profile(name)
            for skill in profile.skills:
                self.assertTrue((SKILLS / skill / "SKILL.md").is_file(), f"{name}: {skill}")
            for custom in profile.custom.values():
                tomllib.loads(custom.read_text(encoding="utf-8"))

    def test_a_profile_is_a_folder_with_no_code(self):
        with tempfile.TemporaryDirectory() as temp:
            profiles = Path(temp)
            write(profiles / "qa" / "profile.toml", 'agent = "tool"\nskills = ["bmad", "x"]\n')
            write(profiles / "qa" / "answers.toml", '[modules.m]\n"a.b" = "1"\n[modules.m.c]\nd = "2"\n')
            write(profiles / "qa" / "custom" / "x.toml", "[agent]\n")
            profile = init.load_profile("qa", profiles)
            self.assertEqual(profile.skills, ("bmad", "x"))
            self.assertEqual(profile.agent, "tool")
            self.assertEqual(profile.answers, {("m", "a.b"): "1", ("m", "c.d"): "2"})
            self.assertEqual(list(profile.custom), ["x.toml"])

    def test_bad_profiles_are_errors_that_name_the_problem(self):
        with tempfile.TemporaryDirectory() as temp:
            profiles = Path(temp)
            write(profiles / "nobmad" / "profile.toml", 'skills = ["x"]\n')
            write(profiles / "good" / "profile.toml", 'skills = ["bmad"]\n')
            for name in ("missing", "../good", "nobmad"):
                with self.subTest(name=name), self.assertRaises(init.InitError):
                    init.load_profile(name, profiles)
            with self.assertRaisesRegex(init.InitError, "available: good, nobmad"):
                init.load_profile("missing", profiles)

    def test_resolve_answers_prefers_the_profile_then_the_default_then_fails(self):
        questions = [
            {"module": "m", "key": "a", "prompt": "A?", "default": "da", "scope": "team"},
            {"module": "m", "key": "b", "prompt": "B?", "default": "db", "scope": "user"},
        ]
        answers = init.resolve_answers(questions, profile_with(answers={("m", "a"): "mine"}))
        self.assertEqual(answers, {"m": {"a": "mine", "b": "db"}})
        questions.append({"module": "m", "key": "c", "prompt": "C?", "default": "", "scope": "team"})
        with self.assertRaisesRegex(init.InitError, r"m\.c"):
            init.resolve_answers(questions, profile_with())

    def test_answers_toml_round_trips_awkward_values(self):
        answers = {"m": {"a.b": 'say "hi"\\n', "c": "ünï"}}
        self.assertEqual(tomllib.loads(init.answers_toml(answers))["modules"], answers)


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
            '[[bmod.config_questions]]\nkey = "site"\nprompt = "Site?"\ndefault = ""\n\n'
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

    def profile(self, tmp_name: str = "profile", **overrides):
        custom = write(self.temp / tmp_name / "bmad-agent-pm.toml", '[agent]\npersistent_facts = ["fact"]\n')
        base = {"answers": {("extra", "site"): "example"}, "custom": {custom.name: custom}}
        return profile_with(**{**base, **overrides})

    def run_init(self, project: Path, version, **kwargs):
        return init.run_init(project, kwargs.pop("profile", None) or self.profile(), "tool", version, self.installer)

    def state(self, project: Path) -> dict[str, bytes]:
        return {
            p.relative_to(project).as_posix(): p.read_bytes()
            for p in sorted(project.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(project).parts
        }

    def test_a_repo_with_no_runtime_is_created_from_the_profile(self):
        report = self.run_init(self.project, self.v1)

        self.assertEqual(report.mode, "create")
        self.assertEqual(self.calls, [["bmad", "bmod-core-tools", "bmod-extra", "extra-skill"]])
        config = tomllib.loads((self.project / "_bmad" / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["modules"]["extra"]["greeting"], "hello")
        self.assertEqual(config["modules"]["extra"]["site"], "example")
        user = tomllib.loads((self.project / "_bmad" / "custom" / "config.user.toml").read_text(encoding="utf-8"))
        self.assertEqual(user["modules"]["extra"]["nick"], "me")
        self.assertEqual(sorted(report.answers_added), ["extra.greeting", "extra.nick", "extra.site"])
        self.assertTrue((self.project / "_bmad" / "custom" / "bmad-agent-pm.toml").is_file())
        self.assertEqual(init.read_pin(self.project), self.v1)
        ignored = (self.project / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertTrue({"_bmad/render/", "*.user.toml"} <= set(ignored))
        self.assertTrue(report.check["current"], report.check)

    def test_a_question_with_no_answer_and_no_default_stops_before_anything_is_written(self):
        with self.assertRaisesRegex(init.InitError, r"extra\.site"):
            self.run_init(self.project, self.v1, profile=self.profile(answers={}))
        self.assertFalse((self.project / "_bmad").exists())

    def test_running_it_again_changes_nothing(self):
        self.run_init(self.project, self.v1)
        commit_all(self.project)
        before = self.state(self.project)

        report = self.run_init(self.project, self.v1)

        self.assertEqual(report.mode, "join")
        self.assertEqual(self.calls[1:], [])
        self.assertEqual(self.state(self.project), before)
        self.assertEqual(git(self.project, "status", "--porcelain"), "")

    def test_a_new_member_keeps_shared_files_and_installs_only_what_is_missing(self):
        self.run_init(self.project, self.v1)
        commit_all(self.project)
        member = self.temp / "member"
        subprocess.run(["git", "clone", "-q", str(self.project), str(member)], check=True)
        shutil.rmtree(member / ".tool" / "skills" / "extra-skill")
        write(member / "_bmad" / "custom" / "bmad-agent-pm.toml", '[agent]\npersistent_facts = ["edited"]\n')
        self.calls.clear()

        report = self.run_init(member, self.v1, profile=self.profile("other", answers={("extra", "site"): "ignored"}))

        self.assertEqual(report.mode, "join")
        self.assertEqual(self.calls, [["extra-skill"]])
        shared = (member / "_bmad" / "custom" / "bmad-agent-pm.toml").read_text(encoding="utf-8")
        self.assertIn("edited", shared)
        config = tomllib.loads((member / "_bmad" / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["modules"]["extra"]["site"], "example")
        self.assertEqual(init.read_pin(member), self.v1)

    def test_a_newer_harness_reinstalls_every_skill_and_moves_the_pin(self):
        self.run_init(self.project, self.v1)
        commit_all(self.project)
        self.calls.clear()

        report = self.run_init(self.project, self.v2)

        self.assertEqual(report.mode, "update")
        self.assertEqual(self.calls, [["bmad", "bmod-core-tools", "bmod-extra", "extra-skill"]])
        self.assertEqual(init.read_pin(self.project), self.v2)
        self.assertEqual(git(self.project, "status", "--porcelain"), "M _bmad/custom/config.toml")

    def test_an_older_harness_is_refused_and_nothing_changes(self):
        self.run_init(self.project, self.v2)
        commit_all(self.project)
        self.calls.clear()

        with self.assertRaisesRegex(init.InitError, "v1.1.0"):
            self.run_init(self.project, self.v1)

        self.assertEqual(self.calls, [])
        self.assertEqual(git(self.project, "status", "--porcelain"), "")

    def test_a_runtime_with_no_pin_is_joined_and_given_none(self):
        self.run_init(self.project, self.v1)
        (self.project / init.PIN_FILE).unlink()

        report = self.run_init(self.project, self.v2)

        self.assertEqual(report.mode, "join")
        self.assertFalse((self.project / init.PIN_FILE).exists())
        self.assertTrue(any("no [harness] pin" in warning for warning in report.warnings))

    def test_running_without_a_ref_warns_that_it_is_on_the_default_branch(self):
        report = self.run_init(self.project, init.Version(None, None, SHA_A))
        self.assertTrue(any("default branch" in warning for warning in report.warnings))
        self.assertEqual(init.read_pin(self.project).sha, SHA_A)

    def test_a_skill_the_installer_did_not_provide_is_an_error(self):
        with self.assertRaisesRegex(init.InitError, "no-such-skill"):
            self.run_init(
                self.project, self.v1, profile=self.profile(skills=("bmad", "bmod-core-tools", "no-such-skill"))
            )


if __name__ == "__main__":
    unittest.main()
