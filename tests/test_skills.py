"""The skill surface: frontmatter, hand-offs, links, and the behaviour rules each skill must carry.

bin/selftest.sh checks that the three skills exist and end in `## Next` (AGENTS.md rule 9). These
tests check what a newcomer depends on: the skill tells the agent how to talk to them, finds where
they are before acting, and stops for them at the judgement calls.
"""
import os
import re

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_DIR = os.path.join(ROOT, "skills")
SKILLS = ["self-healing-audit", "self-healing-build", "self-healing-install-trigger"]
# The README's methodology step each skill implements; descriptions and README must agree.
STEP = {"self-healing-audit": "Step 1", "self-healing-build": "Step 3a", "self-healing-install-trigger": "Step 3b"}


def read_skill(name):
    with open(os.path.join(SKILLS_DIR, name, "SKILL.md"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m, f"{name}: SKILL.md must open with a YAML frontmatter block"
    return yaml.safe_load(m.group(1)), m.group(2)


def section(body, heading):
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    return m.group(1) if m else None


def test_skill_folders_match_the_expected_set():
    found = sorted(d for d in os.listdir(SKILLS_DIR) if os.path.isdir(os.path.join(SKILLS_DIR, d)))
    assert found == sorted(SKILLS)


@pytest.mark.parametrize("name", SKILLS)
def test_frontmatter(name):
    fm, _ = read_skill(name)
    assert fm["name"] == name
    assert isinstance(fm.get("description"), str) and len(fm["description"]) >= 40
    assert fm.get("argument-hint")
    assert isinstance(fm.get("allowed-tools"), list) and fm["allowed-tools"]


@pytest.mark.parametrize("name", SKILLS)
def test_description_names_the_same_step_as_the_readme(name):
    fm, _ = read_skill(name)
    assert fm["description"].startswith(f"{STEP[name]} of the four-step healwright methodology")
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        readme = fh.read()
    number = STEP[name].split()[1]
    row = next((ln for ln in readme.splitlines() if ln.startswith(f"| {number}. ")), None)
    assert row and f"/{name}" in row, f"README methodology table row {number} must name /{name}"


@pytest.mark.parametrize("name", SKILLS)
def test_section_order(name):
    _, body = read_skill(name)
    headings = re.findall(r"^## (.+)$", body, re.M)
    assert headings == ["Working with the user", "Steps", "Done when", "Next"]


@pytest.mark.parametrize("name", SKILLS)
def test_narration_contract(name):
    _, body = read_skill(name)
    work = section(body, "Working with the user")
    assert work, f"{name}: missing '## Working with the user'"
    assert re.search(r"Before each \w+, one line of what", work), f"{name}: say what and why before each action"
    assert "after it, one line with" in work, f"{name}: say the result after each action"
    assert "Every question says what it decides and what each answer changes" in work


@pytest.mark.parametrize("name", SKILLS)
def test_first_step_finds_where_you_are(name):
    _, body = read_skill(name)
    steps = section(body, "Steps")
    first = re.search(r"^0\. \*\*(.+?)\*\*", steps, re.M)
    assert first and first.group(1) == "Find where you are."


@pytest.mark.parametrize("name", ["self-healing-build", "self-healing-install-trigger"])
def test_human_only_steps_are_named_with_a_reason(name):
    _, body = read_skill(name)
    m = re.search(r"^### Only you can do these\n(.*?)(?=^#|\Z)", body, re.S | re.M)
    assert m, f"{name}: missing '### Only you can do these'"
    items = re.findall(r"^- \*\*(.+?)\*\*(.+)$", m.group(1), re.M)
    assert len(items) >= 2
    for what, why in items:
        assert ":" in why, f"{name}: '{what}' needs a one-line reason after a colon"


def test_audit_stops_for_the_human_before_writing():
    _, body = read_skill("self-healing-audit")
    steps = section(body, "Steps")
    checkpoint = steps.find("**Checkpoint: the human confirms the judgement calls.**")
    write = steps.find("**Write `JOB_AUDIT.md`**")
    assert 0 <= checkpoint < write, "the confirmation checkpoint must come before JOB_AUDIT.md is written"
    assert "Do not write the file until this is done" in steps


@pytest.mark.parametrize("name", SKILLS)
def test_next_names_real_skills(name):
    _, body = read_skill(name)
    nxt = section(body, "Next")
    named = set(re.findall(r"`/(self-healing-[a-z-]+)`", nxt))
    assert named, f"{name}: '## Next' must name the next skill"
    assert named <= set(SKILLS), f"{name}: '## Next' names unknown skills {named - set(SKILLS)}"


@pytest.mark.parametrize("name", SKILLS)
def test_skills_that_write_files_may_write(name):
    fm, body = read_skill(name)
    steps = section(body, "Steps")
    if re.search(r"\b(Write|write|Record|record|Copy|copy)\b", steps):
        assert {"Write", "Edit"} & set(fm["allowed-tools"]), f"{name}: its steps write files but allowed-tools cannot"


@pytest.mark.parametrize("name", SKILLS)
def test_healwright_paths_a_skill_cites_exist(name):
    """Skills cite healwright docs by repo path; a renamed doc must not leave a dead pointer."""
    _, body = read_skill(name)
    for path in set(re.findall(r"`((?:docs|examples|tools|templates)/[\w./-]+)`", body)):
        assert os.path.exists(os.path.join(ROOT, path)), f"{name}: cites {path}, which does not exist"


def test_relative_links_in_user_docs_resolve():
    files = [os.path.join(ROOT, "README.md")] + [
        os.path.join(ROOT, "docs", f) for f in os.listdir(os.path.join(ROOT, "docs")) if f.endswith(".md")
    ]
    for path in files:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
            if re.match(r"^[a-z]+://", target):
                continue
            resolved = os.path.normpath(os.path.join(os.path.dirname(path), target))
            assert os.path.exists(resolved), f"{os.path.relpath(path, ROOT)}: broken link {target}"
