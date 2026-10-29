"""Portable collector contracts: profile identity, policy errors and exact-head evidence."""
import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_pr_acceptance as gate

SHA = "a" * 40
PR_URL = "https://github.com/acme/repo/pull/7"
RULES = "repos/acme/repo/rules/branches/main?per_page=100"
PLAN_MESSAGE = "Upgrade to GitHub Pro or make this repository public to enable this feature."


@pytest.fixture
def routed_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    profile = launch / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("", encoding="utf-8")
    # Only synthetic credentials; never inspect or copy the operator's login.
    monkeypatch.setenv("GH_TOKEN", "ambient-gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-token")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "ambient-gh"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ambient-xdg"))
    return profile


def _login(directory):
    directory.mkdir(parents=True)
    (directory / "hosts.yml").write_text("{}\n", encoding="utf-8")


@pytest.mark.parametrize("sandbox,legacy", [(True, False), (False, True), (True, True), (False, False)])
def test_profile_login_location_and_ambient_isolation(routed_profile, sandbox, legacy):
    sandbox_dir = routed_profile / "home" / ".config" / "gh"
    legacy_dir = routed_profile / "gh"
    if sandbox:
        _login(sandbox_dir)
    if legacy:
        _login(legacy_dir)
    before = dict(os.environ)
    env = gate._gh_env(str(routed_profile))
    assert env["GH_CONFIG_DIR"] == str(sandbox_dir if sandbox else legacy_dir)
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert env["HERMES_HOME"] == str(routed_profile)
    assert dict(os.environ) == before


def test_sandbox_without_hosts_keeps_legacy_fallback(routed_profile):
    (routed_profile / "home" / ".config" / "gh").mkdir(parents=True)
    _login(routed_profile / "gh")
    assert gate._gh_env(str(routed_profile))["GH_CONFIG_DIR"] == str(routed_profile / "gh")


@pytest.mark.parametrize("key", ["GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"])
def test_empty_profile_override_cannot_enable_ambient_login(routed_profile, key):
    _login(routed_profile / "home" / ".config" / "gh")
    (routed_profile / ".env").write_text(f"{key}=\n", encoding="utf-8")
    assert gate._gh_env(str(routed_profile))["GH_CONFIG_DIR"] == str(routed_profile / "home" / ".config" / "gh")


@pytest.mark.parametrize("overrides", [
    {"GH_TOKEN": "profile-gh"}, {"GITHUB_TOKEN": "profile-github"},
    {"GH_CONFIG_DIR": "/synthetic/explicit-gh"},
    {"GH_TOKEN": "profile-gh", "GITHUB_TOKEN": "profile-github", "GH_CONFIG_DIR": "/synthetic/explicit-gh"},
])
def test_explicit_profile_env_has_precedence(routed_profile, overrides):
    _login(routed_profile / "home" / ".config" / "gh")
    _login(routed_profile / "gh")
    (routed_profile / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in overrides.items()), encoding="utf-8")
    before = dict(os.environ)
    env = gate._gh_env(str(routed_profile))
    assert {key: env[key] for key in overrides} == overrides
    for key in {"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"} - overrides.keys():
        assert key not in env
    assert dict(os.environ) == before


@pytest.fixture
def evidence(monkeypatch):
    run = {"id": 42, "name": "required", "head_sha": SHA, "app": {"id": 1},
           "status": "completed", "conclusion": "success", "html_url": PR_URL + "/checks"}
    values = {
        "graphql": {"data": {"repository": {"pullRequest": {
            "headRefOid": SHA, "baseRefName": "main", "state": "OPEN",
            "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                {"context": "required", "app": {"databaseId": 1}}]}}}}}},
        RULES: [[]],
        f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=latest": [
            {"total_count": 1, "check_runs": [run]}],
        f"repos/acme/repo/commits/{SHA}/statuses?per_page=100": [[]],
        "repos/acme/repo/pulls/7": {"head": {"sha": SHA}, "base": {"ref": "main"}, "state": "open"},
    }
    calls = []

    def respond(command, **kwargs):
        calls.append((command, kwargs))
        value = values[command[2]]
        if isinstance(value, Exception):
            raise value
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(value), stderr="")

    monkeypatch.setattr(gate.subprocess, "run", respond)
    return values, calls


def _refusal(status, body, stderr_message="private stderr marker"):
    return subprocess.CalledProcessError(1, ["gh", "api"], output=body,
                                         stderr=f"gh: {stderr_message} (HTTP {status})")


@pytest.mark.parametrize("classic_checks", [True, False])
@pytest.mark.parametrize("slurped", [True, False])
def test_plan_unavailable_uses_all_exact_head_ci_and_merged_pr(evidence, classic_checks, slurped):
    values, calls = evidence
    if not classic_checks:
        values["graphql"]["data"]["repository"]["pullRequest"]["baseRef"] = None
    body = {"message": PLAN_MESSAGE, "private": "private body marker"}
    values[RULES] = _refusal(403, json.dumps([body] if slurped else body))
    values[f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=all"] = values.pop(
        f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=latest")
    values["repos/acme/repo/pulls/7"].update(state="closed", merged=True)
    receipt = gate.collect_acceptance("acme/repo", PR_URL)
    assert receipt["ok"] is True and receipt["classification"] == "success"
    assert receipt["policy_classification"] == "feature_unavailable"
    assert receipt["policy_source"] == "exact_head_ci"
    assert receipt["head_sha"] == SHA
    assert {c["id"] for c in receipt["checks"]} == {42}
    assert len(calls) == 5
    assert calls[1][0][-2:] == ["--paginate", "--slurp"]
    serialized = json.dumps(receipt)
    assert "private stderr marker" not in serialized and "private body marker" not in serialized


def test_slurp_envelope_without_pagination_is_auth(evidence):
    evidence[0][RULES] = _refusal(403, json.dumps([{"message": PLAN_MESSAGE}]))
    with pytest.raises(gate._GateAuthError):
        gate._api(RULES, paginate=False)
    assert "--slurp" not in evidence[1][-1][0]


@pytest.mark.parametrize("status,body", [
    (401, json.dumps({"message": PLAN_MESSAGE})),
    (404, json.dumps({"message": PLAN_MESSAGE})),
    (401, json.dumps([{"message": PLAN_MESSAGE}])),
    (404, json.dumps([{"message": PLAN_MESSAGE}])),
    (403, "not JSON: " + PLAN_MESSAGE),
    (403, json.dumps({"message": "Resource not accessible by personal access token"})),
    (403, json.dumps({"message": PLAN_MESSAGE + " extra"})),
    (403, json.dumps({"message": PLAN_MESSAGE.rstrip(".")})),
    (403, json.dumps({"error": PLAN_MESSAGE})),
    (403, json.dumps({"message": None})),
    (403, "[]"),
    (403, json.dumps([{"message": PLAN_MESSAGE}, {"message": PLAN_MESSAGE}])),
    (403, json.dumps([{"message": PLAN_MESSAGE}, None])),
    (403, json.dumps([None, {"message": PLAN_MESSAGE}])),
    (403, json.dumps([[{"message": PLAN_MESSAGE}]])),
    (403, json.dumps([{"message": "Unknown rules error"}])),
    (403, json.dumps([{"error": PLAN_MESSAGE}])),
    (403, json.dumps([PLAN_MESSAGE])),
    (403, "[null]"),
    (403, '[{"message":' + json.dumps(PLAN_MESSAGE) + '}'),
    (403, "null"), (403, "42"), (403, ""),
])
def test_unknown_or_malformed_denial_is_auth_fail_closed(evidence, status, body):
    values, calls = evidence
    values[RULES] = _refusal(status, body, PLAN_MESSAGE + " private stderr marker")
    receipt = gate.collect_acceptance("acme/repo", PR_URL)
    assert receipt["classification"] == "auth" and receipt["ok"] is False
    assert len(calls) == 2
    assert "private stderr marker" not in json.dumps(receipt)


@pytest.mark.parametrize("slurped", [True, False])
@pytest.mark.parametrize("endpoint", ["graphql", "repos/acme/repo/pulls/7",
    f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=latest",
    f"repos/acme/repo/commits/{SHA}/statuses?per_page=100"])
def test_known_message_on_wrong_endpoint_is_auth(evidence, endpoint, slurped):
    values, _ = evidence
    body = {"message": PLAN_MESSAGE}
    values[endpoint] = _refusal(403, json.dumps([body] if slurped else body))
    receipt = gate.collect_acceptance("acme/repo", PR_URL)
    assert receipt["classification"] == "auth" and receipt["ok"] is False


@pytest.mark.parametrize("outcome,expected", [
    ("success", "success"), ("failure", "failure"), ("pending", "pending"),
    ("cancelled", "infra"), ("timed_out", "infra"), ("action_required", "infra"),
    ("neutral", "infra"), ("skipped", "infra"), (None, "infra"),
])
def test_exact_head_required_outcomes(evidence, outcome, expected):
    values, _ = evidence
    run = values[f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=latest"][0]["check_runs"][0]
    run.update(conclusion=outcome, status="in_progress" if outcome == "pending" else "completed")
    receipt = gate.collect_acceptance("acme/repo", PR_URL)
    assert receipt["classification"] == expected
    assert receipt["ok"] is (expected == "success")
    assert receipt["checks"][0]["head_sha"] == SHA


@pytest.mark.parametrize("fault,expected", [
    ("stale_run", "stale"), ("wrong_app", "missing"), ("missing_run", "missing"),
    ("incomplete_pages", "infra"), ("duplicate_pages", "infra"),
    ("head_changed", "stale"), ("base_changed", "stale"), ("closed", "stale"),
])
def test_required_checks_and_reread_guards(evidence, fault, expected):
    values, _ = evidence
    pages = values[f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=latest"]
    run = pages[0]["check_runs"][0]
    current = values["repos/acme/repo/pulls/7"]
    # A successful legacy status cannot bypass an app-pinned required check.
    values[f"repos/acme/repo/commits/{SHA}/statuses?per_page=100"] = [[
        {"id": 99, "context": "required", "state": "success"}]]

    def duplicate_pages():
        pages[0]["total_count"] = 2
        pages.append(copy.deepcopy(pages[0]))

    mutations = {
        "stale_run": lambda: run.update(head_sha="b" * 40),
        "wrong_app": lambda: run["app"].update(id=2),
        "missing_run": lambda: pages[0].update(check_runs=[], total_count=0),
        "incomplete_pages": lambda: pages[0].update(total_count=2),
        "duplicate_pages": duplicate_pages,
        "head_changed": lambda: current["head"].update(sha="b" * 40),
        "base_changed": lambda: current["base"].update(ref="other"),
        "closed": lambda: current.update(state="closed"),
    }
    mutations[fault]()
    receipt = gate.collect_acceptance("acme/repo", PR_URL)
    assert receipt["ok"] is False and receipt["classification"] == expected


def test_rules_and_check_runs_paginate_and_all_reads_use_assignee(evidence, routed_profile):
    values, calls = evidence
    _login(routed_profile / "home" / ".config" / "gh")
    values[RULES] = [[], [{"type": "required_status_checks", "parameters": {
        "required_status_checks": [{"context": "rules-required", "integration_id": 2}]}}]]
    pages = values[f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=latest"]
    pages[0]["total_count"] = 2
    second_run = {**pages[0]["check_runs"][0], "id": 43, "name": "rules-required", "app": {"id": 2}}
    pages.append({"total_count": 2, "check_runs": [second_run]})
    receipt = gate.collect_acceptance("acme/repo", PR_URL, assignee="worker")
    assert receipt["ok"] is True
    assert {check["id"] for check in receipt["checks"]} == {42, 43}
    for command, kwargs in calls:
        assert kwargs["env"]["GH_CONFIG_DIR"] == str(routed_profile / "home" / ".config" / "gh")
        assert "GH_TOKEN" not in kwargs["env"] and "GITHUB_TOKEN" not in kwargs["env"]
        if any(kind in command[2] for kind in ("/rules/", "/check-runs", "/statuses")):
            assert command[-2:] == ["--paginate", "--slurp"]


def test_no_required_checks_cannot_complete(evidence):
    values, calls = evidence
    values["graphql"]["data"]["repository"]["pullRequest"]["baseRef"] = None
    receipt = gate.collect_acceptance("acme/repo", PR_URL)
    assert receipt["ok"] is False and receipt["classification"] == "missing"
    assert receipt["required"] == []
    assert [call[0][2] for call in calls] == ["graphql", RULES]


@pytest.mark.parametrize("contract,published", [("acme/repo", None), ("acme/repo", PR_URL.replace("acme", "other")),
    (PR_URL, PR_URL.replace("/7", "/8"))])
def test_contract_cannot_be_omitted_or_substituted(evidence, contract, published):
    _, calls = evidence
    receipt = gate.collect_acceptance(contract, published)
    assert receipt["ok"] is False and receipt["classification"] == "missing"
    assert calls == []


@pytest.mark.parametrize("endpoint", ["repos/acme/repo/rules", "repos/acme/repo/rulesets",
    "repos/acme/repo/rules/branches/", "repos/acme/repo/rules/branches/main/other"])
def test_similar_endpoint_cannot_be_treated_as_plan_limitation(evidence, endpoint):
    with pytest.raises(gate._GateAuthError):
        evidence[0][endpoint] = _refusal(403, json.dumps({"message": PLAN_MESSAGE}))
        gate._api(endpoint)


@pytest.mark.parametrize("status", [400, 429, 500])
def test_plan_message_without_http403_is_infra(evidence, status):
    evidence[0][RULES] = _refusal(status, json.dumps({"message": PLAN_MESSAGE}))
    receipt = gate.collect_acceptance("acme/repo", PR_URL)
    assert receipt["classification"] == "infra" and receipt["ok"] is False
    assert "private stderr marker" not in json.dumps(receipt)


@pytest.mark.parametrize("app_id", [None, -1])
@pytest.mark.parametrize("latest_state,expected", [("success", "success"), ("failure", "failure")])
def test_unpinned_context_uses_latest_paginated_legacy_status(evidence, app_id, latest_state, expected):
    values, _ = evidence
    required = values["graphql"]["data"]["repository"]["pullRequest"]["baseRef"]["branchProtectionRule"]["requiredStatusChecks"][0]
    required["app"] = {"databaseId": app_id}
    values[f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=latest"] = [
        {"total_count": 0, "check_runs": []}]
    values[f"repos/acme/repo/commits/{SHA}/statuses?per_page=100"] = [
        [{"id": 2, "context": "required", "state": latest_state}],
        [{"id": 1, "context": "required", "state": "pending"}]]
    receipt = gate.collect_acceptance("acme/repo", PR_URL)
    assert receipt["classification"] == expected
    assert receipt["ok"] is (expected == "success")
    assert receipt["checks"][0]["id"] == 2


def test_optional_failure_does_not_veto_required_success(evidence):
    pages = evidence[0][f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=latest"]
    pages[0]["check_runs"].append({**pages[0]["check_runs"][0], "id": 43,
                                   "name": "optional", "conclusion": "failure"})
    pages[0]["total_count"] = 2
    assert gate.collect_acceptance("acme/repo", PR_URL)["ok"] is True


@pytest.mark.parametrize("patch", [{"headRefOid": "not-a-sha"}, {"state": "CLOSED"}])
def test_initial_pr_must_have_valid_head_and_state(evidence, patch):
    values, calls = evidence
    values["graphql"]["data"]["repository"]["pullRequest"].update(patch)
    receipt = gate.collect_acceptance("acme/repo", PR_URL)
    assert receipt["classification"] == "infra" and receipt["ok"] is False
    assert len(calls) == 1


def test_graphql_errors_cannot_supply_partial_evidence(evidence):
    evidence[0]["graphql"]["errors"] = [{"message": "private error marker"}]
    receipt = gate.collect_acceptance("acme/repo", PR_URL)
    assert receipt["classification"] == "infra" and receipt["ok"] is False
    assert "private error marker" not in json.dumps(receipt)


@pytest.fixture
def plan_evidence(evidence):
    values, calls = evidence
    values["graphql"]["data"]["repository"]["pullRequest"]["baseRef"] = None
    values[RULES] = _refusal(403, json.dumps([{"message": PLAN_MESSAGE}]))
    values[f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=all"] = values.pop(
        f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=latest")
    values["repos/acme/repo/pulls/7"].update(state="closed", merged=True)
    return values, calls


@pytest.mark.parametrize("outcome,status", [
    ("failure", "completed"), (None, "queued"), (None, "in_progress"),
    ("skipped", "completed"), ("neutral", "completed"), ("cancelled", "completed"),
    ("timed_out", "completed"), ("action_required", "completed"),
])
def test_plan_unavailable_refuses_any_unsuccessful_run(plan_evidence, outcome, status):
    values, _ = plan_evidence
    page = values[f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=all"][0]
    page["check_runs"].append({**page["check_runs"][0], "id": 43, "name": "extra",
                               "conclusion": outcome, "status": status})
    page["total_count"] = 2
    receipt = gate.collect_acceptance("acme/repo", PR_URL)
    assert not receipt["ok"]
    assert any(c["id"] == 43 and c["classification"] != "success" for c in receipt["checks"])


@pytest.mark.parametrize("state", ["success", "pending", "failure", "error", "unknown"])
def test_plan_unavailable_checks_every_legacy_status(plan_evidence, state):
    values, _ = plan_evidence
    # Even an older unsuccessful status for the same context may not disappear
    # behind the latest success on the plan-limited compatibility path.
    values[f"repos/acme/repo/commits/{SHA}/statuses?per_page=100"] = [
        [{"id": 44, "context": "legacy", "state": "success"}],
        [{"id": 43, "context": "legacy", "state": state}]]
    receipt = gate.collect_acceptance("acme/repo", PR_URL)
    assert receipt["ok"] is (state == "success")
    assert {c["id"] for c in receipt["checks"]} == {42, 43, 44}


def test_plan_unavailable_status_only_can_supply_ci_evidence(plan_evidence):
    values, _ = plan_evidence
    values[f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=all"] = [
        {"total_count": 0, "check_runs": []}]
    values[f"repos/acme/repo/commits/{SHA}/statuses?per_page=100"] = [
        [{"id": 43, "context": "legacy", "state": "success"}]]
    assert gate.collect_acceptance("acme/repo", PR_URL)["ok"] is True


@pytest.mark.parametrize("change,expected", [
    ({"state": "open", "merged": False}, "pending"),
    ({"state": "closed", "merged": False}, "stale"),
    ({"state": "closed"}, "stale"),
    ({"head": {"sha": "b" * 40}}, "stale"),
    ({"base": {"ref": "other"}}, "stale"),
])
def test_plan_unavailable_requires_merged_expected_head(plan_evidence, change, expected):
    values, _ = plan_evidence
    current = values["repos/acme/repo/pulls/7"]
    if change == {"state": "closed"}:
        current.pop("merged")
    current.update(change)
    receipt = gate.collect_acceptance("acme/repo", PR_URL)
    assert not receipt["ok"] and receipt["classification"] == expected


@pytest.mark.parametrize("fault", ["stale", "incomplete", "no_checks"])
def test_plan_unavailable_refuses_incomplete_head_evidence(plan_evidence, fault):
    values, _ = plan_evidence
    page = values[f"repos/acme/repo/commits/{SHA}/check-runs?per_page=100&filter=all"][0]
    if fault == "stale":
        page["check_runs"][0]["head_sha"] = "b" * 40
    elif fault == "incomplete":
        page["total_count"] = 2
    else:
        page.update(total_count=0, check_runs=[])
    assert not gate.collect_acceptance("acme/repo", PR_URL)["ok"]


@pytest.mark.parametrize("endpoint,paginate", [
    ("repos/acme/repo/rules/branches/main?per_page=100", True),
    ("repos/acme/repo/branches/main/protection", False),
])
def test_exact_plan_error_is_scoped_to_policy_endpoints(evidence, endpoint, paginate):
    evidence[0][endpoint] = _refusal(403, json.dumps({"message": PLAN_MESSAGE}))
    with pytest.raises(gate._GateFeatureUnavailableError):
        gate._api(endpoint, paginate=paginate)
