"""Round-trip and storage tests for src/cua/artifact (schema + SQLite store)."""

from __future__ import annotations

from cua.artifact import (
    Artifact,
    Condition,
    InputParam,
    LocatorSpec,
    OutcomeRule,
    OutputField,
    Step,
    list_artifacts,
    list_events,
    load_artifact,
    match_event,
    propose_event,
    review_event,
    save_artifact,
)


def _sample_artifact(artifact_id: str = "cu-read-savings-balance", version: int = 1) -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        name="Read savings balance",
        version=version,
        goal_description="Look up a member and read their current savings balance.",
        app_id="cu-servicing-console",
        base_url_pattern="http://127.0.0.1:5055",
        inputs=[InputParam(name="member_id", type="string", required=True, description="e.g. M-10001")],
        outputs=[OutputField(name="savings_balance", type="number", description="Current savings balance")],
        steps=[
            Step(
                step_id="search",
                action_type="type_text",
                locator_chain=(LocatorSpec(by="name", value="member_query"),),
                post_condition=Condition(kind="url_matches", value="/search"),
                input_binding="member_id",
            ),
            Step(
                step_id="open_member",
                action_type="click",
                locator_chain=(
                    LocatorSpec(by="href", value="/member/{member_id}"),
                    LocatorSpec(by="role_text", value="M-10001", tag="a"),
                ),
                post_condition=Condition(kind="url_matches", value="/member/"),
            ),
        ],
        checkpoint=Condition(kind="text_contains", value="Savings"),
        outcome_table=[
            OutcomeRule(
                signature=Condition(kind="text_contains", value="Member record not found"),
                outcome_class="business",
                outcome_name="member_not_found",
            )
        ],
        created_from_run="trace-001",
    )


def test_artifact_json_round_trip():
    artifact = _sample_artifact()
    restored = Artifact.from_json(artifact.to_json())
    assert restored == artifact
    assert restored.steps[1].locator_chain[0].by == "href"


def test_save_and_load_latest_version(tmp_path):
    db_path = tmp_path / "cua.db"
    v1 = _sample_artifact(version=1)
    v2 = _sample_artifact(version=2)
    save_artifact(v1, db_path=db_path)
    save_artifact(v2, db_path=db_path)

    latest = load_artifact(v1.artifact_id, db_path=db_path)
    assert latest.version == 2

    pinned = load_artifact(v1.artifact_id, version=1, db_path=db_path)
    assert pinned.version == 1

    all_artifacts = list_artifacts(db_path=db_path)
    assert len(all_artifacts) == 1
    assert all_artifacts[0].version == 2


def test_event_library_pending_rows_never_match(tmp_path):
    db_path = tmp_path / "cua.db"
    condition = Condition(kind="text_contains", value="session has expired")
    signature_id = propose_event(
        condition,
        outcome_class="recoverable",
        outcome_name="session_timeout",
        source_app_id="cu-servicing-console",
        proposed_by="discovery-llm",
        db_path=db_path,
    )

    # Pending: must not be consulted by replay's matcher.
    assert match_event("Your session has expired, please log in again.", "/login", [], db_path=db_path) is None
    assert [e["status"] for e in list_events(db_path=db_path)] == ["pending_review"]

    review_event(signature_id, approve=True, db_path=db_path)

    match = match_event("Your session has expired, please log in again.", "/login", [], db_path=db_path)
    assert match is not None
    assert match["outcome_name"] == "session_timeout"
    assert match["signature_id"] == signature_id


def test_event_library_rejected_rows_never_match(tmp_path):
    db_path = tmp_path / "cua.db"
    condition = Condition(kind="element_present", value="button:Suspicious Action")
    signature_id = propose_event(
        condition,
        outcome_class="hard_failure",
        outcome_name="guess_was_wrong",
        source_app_id="cu-servicing-console",
        proposed_by="discovery-llm",
        db_path=db_path,
    )
    review_event(signature_id, approve=False, db_path=db_path)

    assert match_event("", "/", ["button:Suspicious Action"], db_path=db_path) is None
