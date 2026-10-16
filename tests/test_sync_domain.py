"""x4-2 纯 domain 状态机的行为迁移测试。

这些测试刻意不启动 HTTP、不打开 SQLite，也不接触 Anki collection；它们只验证
上传优先、应用证据、快照许可、分页恢复和覆盖授权的状态/动作边界。
"""

from __future__ import annotations

import copy
import unittest
from typing import List, Optional, Tuple

from inkcards_anki_bridge.domain.sync_gate import (
    AnkiApplicationEvidence,
    ApplicationState,
    ApplyCoordinationFence,
    ApplyFenceStatus,
    BeginSync,
    ChecksumStatus,
    CommitRequest,
    DurableReceipt,
    ErrorCode,
    EventRange,
    ImmutableReviewEvent,
    LUA_SAFE_INTEGER_MAX,
    LocalEvent,
    OpenPullRequest,
    OverrideConfirmRequest,
    OverridePrepareRequest,
    OverrideTokenStatus,
    PersistedState,
    PermitRequest,
    PullAbortReason,
    PullAbortRequest,
    PullPage,
    PullProgress,
    ReceiptQueryReport,
    ReceiptStatus,
    SnapshotEvidence,
    SyncAction,
    SyncContext,
    SyncMachine,
    SyncPhase,
    SyncScope,
    SyncV2Error,
    UploadOutcome,
    UploadReport,
    Watermark,
)


PERMIT_NONCE = "permit-nonce-1"


def context(local_generation: int = 1) -> SyncContext:
    return SyncContext(
        device_id="device-a",
        profile_id="profile-a",
        scope=SyncScope(scope_id="deck:language"),
        session_id="session-a",
        local_generation=local_generation,
    )


def event_range(first_sequence: int, last_sequence: int) -> EventRange:
    return EventRange(first_sequence=first_sequence, last_sequence=last_sequence)


def receipts(
    sync_context: SyncContext,
    selected_range: EventRange,
    status: ReceiptStatus,
) -> List[DurableReceipt]:
    return [
        DurableReceipt(
            event_id=f"{sync_context.session_id}:{sequence}",
            sequence=sequence,
            status=status,
        )
        for sequence in range(
            selected_range.first_sequence,
            selected_range.last_sequence + 1,
        )
    ]


def begin(
    machine: SyncMachine,
    sync_context: SyncContext,
    selected_range: Optional[EventRange],
) -> None:
    machine.begin(
        BeginSync(
            context=sync_context,
            local_events=selected_range,
            applied_watermark=Watermark.known(0),
        )
    )


def upload(
    machine: SyncMachine,
    sync_context: SyncContext,
    selected_range: EventRange,
    outcome: UploadOutcome,
    checksum: ChecksumStatus,
    status: ReceiptStatus,
) -> None:
    machine.record_upload(
        UploadReport(
            context=sync_context,
            range=selected_range,
            outcome=outcome,
            checksum=checksum,
            receipts=receipts(sync_context, selected_range, status),
        )
    )


def apply_all(
    machine: SyncMachine,
    sync_context: SyncContext,
    selected_range: EventRange,
) -> None:
    machine.record_receipts(
        ReceiptQueryReport(
            context=sync_context,
            range=selected_range,
            outcome=UploadOutcome.COMPLETE,
            receipts=receipts(sync_context, selected_range, ReceiptStatus.APPLIED),
        )
    )
    machine.record_anki_application(
        AnkiApplicationEvidence(
            context=sync_context,
            state=ApplicationState.APPLIED,
            applied_watermark=Watermark.known(selected_range.last_sequence),
        )
    )


def snapshot(
    sync_context: SyncContext,
    watermark: int,
    verified: bool,
) -> SnapshotEvidence:
    return SnapshotEvidence(
        context=sync_context,
        snapshot_id="snapshot-1",
        applied_watermark=watermark,
        snapshot_generation=7,
        immutable=verified,
        checksum=(ChecksumStatus.VERIFIED if verified else ChecksumStatus.INVALID),
    )


def applied_machine() -> Tuple[SyncMachine, SyncContext, EventRange]:
    machine = SyncMachine()
    sync_context = context()
    selected_range = event_range(1, 2)
    begin(machine, sync_context, selected_range)
    upload(
        machine,
        sync_context,
        selected_range,
        UploadOutcome.COMPLETE,
        ChecksumStatus.VERIFIED,
        ReceiptStatus.DURABLE_RECEIVED,
    )
    apply_all(machine, sync_context, selected_range)
    return machine, sync_context, selected_range


def ready_machine() -> Tuple[SyncMachine, SyncContext, EventRange, str]:
    machine, sync_context, selected_range = applied_machine()
    permit = machine.accept_snapshot(
        snapshot(sync_context, 2, True),
        PermitRequest(permit_id="permit-1", nonce=PERMIT_NONCE),
    )
    return machine, sync_context, selected_range, permit.permit_id


def quiesced_fence(machine: SyncMachine, fence_id: str) -> ApplyCoordinationFence:
    return machine.current_apply_fence(fence_id, ApplyFenceStatus.QUIESCED)


def prepare_request(
    sync_context: SyncContext,
    selected_range: Optional[EventRange],
    baseline,
    fence: ApplyCoordinationFence,
    token_id: str,
    nonce: str,
    expires_at_ms: int,
) -> OverridePrepareRequest:
    return OverridePrepareRequest(
        context=sync_context,
        scope=sync_context.scope,
        abandoned_events=selected_range,
        baseline=baseline,
        fence=fence,
        token_id=token_id,
        nonce=nonce,
        expires_at_ms=expires_at_ms,
    )


def confirm_request(
    sync_context: SyncContext,
    token_id: str,
    nonce: str,
    fence: ApplyCoordinationFence,
) -> OverrideConfirmRequest:
    return OverrideConfirmRequest(
        context=sync_context,
        scope=sync_context.scope,
        token_id=token_id,
        nonce=nonce,
        fence=fence,
    )


def assert_no_pull_or_commit(
    test_case: unittest.TestCase, machine: SyncMachine
) -> None:
    test_case.assertNotIn(
        machine.next_action().kind,
        {"open_pull", "pull_page", "commit"},
    )


def error_code(test_case: unittest.TestCase, operation) -> ErrorCode:
    with test_case.assertRaises(SyncV2Error) as raised:
        operation()
    return raised.exception.code


class SyncDomainTests(unittest.TestCase):
    def test_persisted_json_rejects_duplicate_keys_and_non_finite_numbers(self) -> None:
        encoded = SyncMachine().state().to_json()
        duplicate = encoded[:-1] + ',"schema_version":1}'
        self.assertEqual(
            error_code(self, lambda: PersistedState.from_json(duplicate)),
            ErrorCode.INVALID_INPUT,
        )
        self.assertEqual(
            error_code(
                self,
                lambda: PersistedState.from_json('{"schema_version":NaN}'),
            ),
            ErrorCode.INVALID_INPUT,
        )

    def test_event_hash_excludes_delivery_state_and_keeps_device_rating_domain(
        self,
    ) -> None:
        event = ImmutableReviewEvent.create(
            "event-1",
            LUA_SAFE_INTEGER_MAX,
            {"rating": 4, "answer": "中文"},
        )
        event.verify_hash()
        self.assertEqual(event.payload["rating"], 4)
        self.assertEqual(
            error_code(
                self,
                lambda: ImmutableReviewEvent.create(
                    "event-2",
                    1,
                    {"rating": 4, "delivery_status": "pending"},
                ),
            ),
            ErrorCode.INVALID_INPUT,
        )
        self.assertEqual(
            error_code(
                self,
                lambda: ImmutableReviewEvent.create("event-3", 1, {"rating": 5}),
            ),
            ErrorCode.INVALID_INPUT,
        )
        self.assertEqual(
            error_code(
                self,
                lambda: ImmutableReviewEvent.create(
                    "event-over-safe", LUA_SAFE_INTEGER_MAX + 1, {"rating": 1}
                ),
            ),
            ErrorCode.INVALID_INPUT,
        )

    def test_persisted_state_is_strict_and_recoverable(self) -> None:
        sync_context = context()
        selected_range = event_range(1, 2)
        machine = SyncMachine()
        begin(machine, sync_context, selected_range)
        upload(
            machine,
            sync_context,
            selected_range,
            UploadOutcome.UNKNOWN,
            ChecksumStatus.VERIFIED,
            ReceiptStatus.DURABLE_RECEIVED,
        )

        encoded = machine.state().to_dict()
        decoded = PersistedState.from_dict(encoded)
        restored = SyncMachine.restore(decoded)
        self.assertEqual(restored.state(), machine.state())
        self.assertEqual(
            restored.next_action(),
            SyncAction.query_receipts(selected_range),
        )

        with_unknown = copy.deepcopy(encoded)
        with_unknown["unexpected_field"] = "reject"
        self.assertEqual(
            error_code(
                self,
                lambda: PersistedState.from_dict(with_unknown),
            ),
            ErrorCode.INVALID_INPUT,
        )

        wrong_schema = copy.deepcopy(encoded)
        wrong_schema["schema_version"] += 1
        self.assertEqual(
            error_code(
                self,
                lambda: SyncMachine.restore(PersistedState.from_dict(wrong_schema)),
            ),
            ErrorCode.INVALID_INPUT,
        )

        ready, _, _, _ = ready_machine()
        forged_gate = copy.deepcopy(ready.state())
        forged_gate.application_state = ApplicationState.PENDING
        self.assertEqual(
            error_code(self, lambda: SyncMachine.restore(forged_gate)),
            ErrorCode.INVALID_STATE,
        )

        forged_snapshot = copy.deepcopy(ready.state())
        forged_snapshot.snapshot.checksum = ChecksumStatus.INVALID
        self.assertEqual(
            error_code(self, lambda: SyncMachine.restore(forged_snapshot)),
            ErrorCode.SNAPSHOT_INVALID,
        )

    def test_forged_pull_complete_progress_cannot_restore_or_commit(self) -> None:
        machine, _, _, permit_id = ready_machine()
        forged = machine.into_state()
        forged.phase = SyncPhase.PULL_COMPLETE
        forged.pull = PullProgress(
            permit_id=permit_id,
            nonce=PERMIT_NONCE,
            snapshot_id="snapshot-1",
            snapshot_generation=7,
            next_page_index=1,
            received_pages=0,
            received_bytes=0,
            complete=True,
        )
        with self.assertRaises(SyncV2Error):
            SyncMachine.restore(forged)

    def test_override_pending_restore_is_exactly_bound_to_current_state(self) -> None:
        machine, sync_context, selected_range, _ = ready_machine()
        baseline = machine.current_override_baseline()
        fence = quiesced_fence(machine, "fence-roundtrip")
        machine.prepare_override(
            prepare_request(
                sync_context,
                selected_range,
                baseline,
                fence,
                "override-roundtrip",
                "nonce-roundtrip",
                2_000,
            ),
            1_000,
        )
        machine.confirm_override(
            confirm_request(
                sync_context, "override-roundtrip", "nonce-roundtrip", fence
            ),
            1_000,
        )
        self.assertEqual(machine.state().phase, SyncPhase.OVERRIDE_PENDING_EXECUTION)
        self.assertEqual(machine.next_action(), SyncAction.none())

        encoded = machine.state().to_dict()
        restored = SyncMachine.restore(PersistedState.from_dict(encoded))
        self.assertEqual(restored.state(), machine.state())

        for mutate in (
            lambda value: setattr(value.context, "session_id", "forged-session"),
            lambda value: setattr(value.context, "local_generation", 2),
            lambda value: setattr(
                value.receipts[0], "status", ReceiptStatus.DURABLE_RECEIVED
            ),
            lambda value: setattr(value, "application_state", ApplicationState.PENDING),
            lambda value: setattr(value, "applied_watermark", Watermark.known(1)),
        ):
            forged = copy.deepcopy(machine.state())
            mutate(forged)
            self.assertEqual(
                forged.override_tokens,
                machine.state().override_tokens,
            )
            with self.assertRaises(SyncV2Error):
                SyncMachine.restore(forged)

        historical = SyncMachine()
        begin(historical, sync_context, selected_range)
        historical.record_upload(
            UploadReport(
                context=sync_context,
                range=selected_range,
                outcome=UploadOutcome.FAILED,
                checksum=ChecksumStatus.UNKNOWN,
                receipts=[],
            )
        )
        historical_baseline = historical.current_override_baseline()
        historical_fence = quiesced_fence(historical, "fence-historical")
        historical.prepare_override(
            prepare_request(
                sync_context,
                selected_range,
                historical_baseline,
                historical_fence,
                "override-historical",
                "nonce-historical",
                2_000,
            ),
            1_000,
        )
        historical.confirm_override(
            confirm_request(
                sync_context,
                "override-historical",
                "nonce-historical",
                historical_fence,
            ),
            1_000,
        )

        current, _, _ = applied_machine()
        forged_phase = current.into_state()
        forged_phase.phase = SyncPhase.OVERRIDE_PENDING_EXECUTION
        forged_phase.override_tokens.append(
            copy.deepcopy(historical.state().override_tokens[0])
        )
        with self.assertRaises(SyncV2Error):
            SyncMachine.restore(forged_phase)

    def test_sync_01_upload_failure_keeps_pull_and_commit_unreachable(self) -> None:
        sync_context = context()
        selected_range = event_range(1, 2)
        machine = SyncMachine()
        begin(machine, sync_context, selected_range)
        self.assertEqual(machine.next_action(), SyncAction.upload(selected_range))
        upload(
            machine,
            sync_context,
            selected_range,
            UploadOutcome.FAILED,
            ChecksumStatus.UNKNOWN,
            ReceiptStatus.DURABLE_RECEIVED,
        )
        self.assertEqual(machine.state().phase, SyncPhase.UPLOAD_REQUIRED)
        self.assertEqual(machine.state().last_error, ErrorCode.UPLOAD_FAILED)
        assert_no_pull_or_commit(self, machine)
        self.assertEqual(
            error_code(
                self,
                lambda: machine.accept_snapshot(
                    snapshot(sync_context, 2, True),
                    PermitRequest("p", "n"),
                ),
            ),
            ErrorCode.INVALID_TRANSITION,
        )

    def test_sync_02_ack_lost_reconciles_same_range_without_download(self) -> None:
        sync_context = context()
        selected_range = event_range(4, 5)
        machine = SyncMachine()
        begin(machine, sync_context, selected_range)
        machine.record_upload(
            UploadReport(
                context=sync_context,
                range=selected_range,
                outcome=UploadOutcome.UNKNOWN,
                checksum=ChecksumStatus.VERIFIED,
                receipts=[],
            )
        )
        self.assertEqual(
            machine.next_action(),
            SyncAction.query_receipts(selected_range),
        )
        self.assertEqual(machine.state().last_error, ErrorCode.UPLOAD_UNKNOWN)
        machine.record_receipts(
            ReceiptQueryReport(
                context=sync_context,
                range=selected_range,
                outcome=UploadOutcome.COMPLETE,
                receipts=receipts(
                    sync_context,
                    selected_range,
                    ReceiptStatus.DURABLE_RECEIVED,
                ),
            )
        )
        self.assertEqual(machine.state().phase, SyncPhase.PENDING_APPLY)
        self.assertEqual(
            machine.next_action(),
            SyncAction.wait_for_anki_apply(selected_range),
        )
        assert_no_pull_or_commit(self, machine)

    def test_sync_03_durable_received_but_anki_offline_is_pending_apply(self) -> None:
        sync_context = context()
        selected_range = event_range(1, 1)
        machine = SyncMachine()
        begin(machine, sync_context, selected_range)
        upload(
            machine,
            sync_context,
            selected_range,
            UploadOutcome.COMPLETE,
            ChecksumStatus.VERIFIED,
            ReceiptStatus.DURABLE_RECEIVED,
        )
        self.assertEqual(machine.state().phase, SyncPhase.PENDING_APPLY)
        self.assertEqual(
            machine.state().application_state,
            ApplicationState.PENDING,
        )
        assert_no_pull_or_commit(self, machine)
        self.assertEqual(
            error_code(
                self,
                lambda: machine.accept_snapshot(
                    snapshot(sync_context, 1, True),
                    PermitRequest("p", "n"),
                ),
            ),
            ErrorCode.INVALID_TRANSITION,
        )

    def test_sync_04_applied_events_with_invalid_snapshot_keep_old_state(self) -> None:
        machine, sync_context, selected_range = applied_machine()
        self.assertEqual(machine.state().phase, SyncPhase.AWAITING_SNAPSHOT)
        self.assertEqual(
            error_code(
                self,
                lambda: machine.accept_snapshot(
                    snapshot(sync_context, selected_range.last_sequence, False),
                    PermitRequest("permit-bad", "nonce-bad"),
                ),
            ),
            ErrorCode.SNAPSHOT_INVALID,
        )
        self.assertEqual(machine.state().phase, SyncPhase.AWAITING_SNAPSHOT)
        self.assertEqual(machine.state().local_events, selected_range)
        assert_no_pull_or_commit(self, machine)

    def test_sync_05_new_local_event_invalidates_paged_pull_and_old_snapshot(
        self,
    ) -> None:
        machine, sync_context, selected_range, permit_id = ready_machine()
        machine.open_pull(OpenPullRequest(sync_context, permit_id, PERMIT_NONCE))
        machine.accept_page(
            PullPage(
                context=sync_context,
                permit_id=permit_id,
                nonce=PERMIT_NONCE,
                snapshot_id="snapshot-1",
                snapshot_generation=7,
                page_index=0,
                page_bytes=64,
                checksum=ChecksumStatus.VERIFIED,
                cursor="cursor-1",
                complete=False,
            )
        )
        new_context = SyncContext(
            device_id=sync_context.device_id,
            profile_id=sync_context.profile_id,
            scope=sync_context.scope,
            session_id=sync_context.session_id,
            local_generation=2,
        )
        machine.record_local_event(
            LocalEvent(context=new_context, sequence=selected_range.last_sequence + 1)
        )
        self.assertEqual(machine.state().phase, SyncPhase.UPLOAD_REQUIRED)
        self.assertEqual(
            machine.state().local_events,
            event_range(
                selected_range.first_sequence, selected_range.last_sequence + 1
            ),
        )
        assert_no_pull_or_commit(self, machine)
        self.assertEqual(
            error_code(
                self,
                lambda: machine.commit(
                    CommitRequest(
                        context=sync_context,
                        permit_id=permit_id,
                        nonce=PERMIT_NONCE,
                        snapshot_id="snapshot-1",
                        snapshot_generation=7,
                    )
                ),
            ),
            ErrorCode.PULL_INCOMPLETE,
        )

    def test_sync_06_partial_gap_checksum_and_unknown_apply_all_fail_closed(
        self,
    ) -> None:
        sync_context = context()
        selected_range = event_range(1, 2)

        partial = SyncMachine()
        begin(partial, sync_context, selected_range)
        upload(
            partial,
            sync_context,
            selected_range,
            UploadOutcome.PARTIAL,
            ChecksumStatus.VERIFIED,
            ReceiptStatus.DURABLE_RECEIVED,
        )
        self.assertEqual(partial.state().phase, SyncPhase.UPLOAD_REQUIRED)
        assert_no_pull_or_commit(self, partial)

        gap = SyncMachine()
        begin(gap, sync_context, selected_range)
        gap.record_upload(
            UploadReport(
                context=sync_context,
                range=selected_range,
                outcome=UploadOutcome.COMPLETE,
                checksum=ChecksumStatus.VERIFIED,
                receipts=receipts(
                    sync_context,
                    event_range(1, 1),
                    ReceiptStatus.DURABLE_RECEIVED,
                ),
            )
        )
        self.assertEqual(gap.state().phase, SyncPhase.RECONCILE_RECEIPTS)
        self.assertEqual(gap.state().last_error, ErrorCode.RECEIPT_MISSING)
        assert_no_pull_or_commit(self, gap)

        checksum = SyncMachine()
        begin(checksum, sync_context, selected_range)
        checksum.record_upload(
            UploadReport(
                context=sync_context,
                range=selected_range,
                outcome=UploadOutcome.UNKNOWN,
                checksum=ChecksumStatus.INVALID,
                receipts=[],
            )
        )
        self.assertEqual(checksum.state().phase, SyncPhase.UPLOAD_REQUIRED)
        self.assertEqual(
            checksum.state().last_error,
            ErrorCode.UPLOAD_CHECKSUM_INVALID,
        )
        assert_no_pull_or_commit(self, checksum)

        unknown_apply = SyncMachine()
        begin(unknown_apply, sync_context, selected_range)
        upload(
            unknown_apply,
            sync_context,
            selected_range,
            UploadOutcome.COMPLETE,
            ChecksumStatus.VERIFIED,
            ReceiptStatus.DURABLE_RECEIVED,
        )
        unknown_apply.record_anki_application(
            AnkiApplicationEvidence(
                context=sync_context,
                state=ApplicationState.UNKNOWN,
                applied_watermark=Watermark.unknown(),
            )
        )
        self.assertEqual(
            unknown_apply.state().phase,
            SyncPhase.RECONCILE_APPLICATION,
        )
        self.assertEqual(
            unknown_apply.next_action(),
            SyncAction.query_anki_watermark(),
        )
        assert_no_pull_or_commit(self, unknown_apply)

    def test_identity_profile_scope_session_and_generation_mismatches_are_rejected(
        self,
    ) -> None:
        sync_context = context()
        selected_range = event_range(1, 1)
        machine = SyncMachine()
        begin(machine, sync_context, selected_range)

        for attribute, value, expected in (
            ("device_id", "other-device", ErrorCode.DEVICE_MISMATCH),
            ("profile_id", "other-profile", ErrorCode.PROFILE_MISMATCH),
            ("session_id", "other-session", ErrorCode.SESSION_MISMATCH),
            ("local_generation", 2, ErrorCode.GENERATION_MISMATCH),
        ):
            observed = copy.deepcopy(sync_context)
            setattr(observed, attribute, value)
            self.assertEqual(
                error_code(
                    self,
                    lambda observed=observed: machine.record_upload(
                        UploadReport(
                            context=observed,
                            range=selected_range,
                            outcome=UploadOutcome.COMPLETE,
                            checksum=ChecksumStatus.VERIFIED,
                            receipts=receipts(
                                sync_context,
                                selected_range,
                                ReceiptStatus.DURABLE_RECEIVED,
                            ),
                        )
                    ),
                ),
                expected,
            )

        observed_scope = copy.deepcopy(sync_context)
        observed_scope.scope.scope_id = "deck:other"
        self.assertEqual(
            error_code(
                self,
                lambda: machine.record_upload(
                    UploadReport(
                        context=observed_scope,
                        range=selected_range,
                        outcome=UploadOutcome.COMPLETE,
                        checksum=ChecksumStatus.VERIFIED,
                        receipts=receipts(
                            sync_context,
                            selected_range,
                            ReceiptStatus.DURABLE_RECEIVED,
                        ),
                    )
                ),
            ),
            ErrorCode.SCOPE_MISMATCH,
        )
        self.assertEqual(machine.state().phase, SyncPhase.UPLOAD_REQUIRED)

    def test_permit_is_bound_and_one_shot(self) -> None:
        machine, sync_context, _, permit_id = ready_machine()
        machine.open_pull(OpenPullRequest(sync_context, permit_id, PERMIT_NONCE))
        machine.accept_page(
            PullPage(
                context=sync_context,
                permit_id=permit_id,
                nonce=PERMIT_NONCE,
                snapshot_id="snapshot-1",
                snapshot_generation=7,
                page_index=0,
                page_bytes=1,
                checksum=ChecksumStatus.VERIFIED,
                cursor=None,
                complete=True,
            )
        )
        machine.commit(
            CommitRequest(
                context=sync_context,
                permit_id=permit_id,
                nonce=PERMIT_NONCE,
                snapshot_id="snapshot-1",
                snapshot_generation=7,
            )
        )
        self.assertEqual(machine.state().phase, SyncPhase.COMMITTED)
        self.assertEqual(machine.next_action(), SyncAction.none())
        self.assertEqual(
            error_code(
                self,
                lambda: machine.open_pull(
                    OpenPullRequest(sync_context, permit_id, PERMIT_NONCE)
                ),
            ),
            ErrorCode.INVALID_TRANSITION,
        )
        self.assertEqual(
            error_code(
                self,
                lambda: machine.commit(
                    CommitRequest(
                        context=sync_context,
                        permit_id=permit_id,
                        nonce=PERMIT_NONCE,
                        snapshot_id="snapshot-1",
                        snapshot_generation=7,
                    )
                ),
            ),
            ErrorCode.PULL_INCOMPLETE,
        )

    def test_permit_nonce_is_bound_across_pull_requests_and_roundtrip(self) -> None:
        machine, sync_context, _, permit_id = ready_machine()
        self.assertEqual(
            error_code(
                self,
                lambda: machine.open_pull(
                    OpenPullRequest(sync_context, permit_id, "wrong-open-nonce")
                ),
            ),
            ErrorCode.PERMIT_INVALID,
        )
        machine.open_pull(OpenPullRequest(sync_context, permit_id, PERMIT_NONCE))
        restored = SyncMachine.restore(
            PersistedState.from_dict(machine.state().to_dict())
        )
        self.assertEqual(restored.state(), machine.state())
        self.assertEqual(
            error_code(
                self,
                lambda: machine.accept_page(
                    PullPage(
                        context=sync_context,
                        permit_id=permit_id,
                        nonce="wrong-page-nonce",
                        snapshot_id="snapshot-1",
                        snapshot_generation=7,
                        page_index=0,
                        page_bytes=1,
                        checksum=ChecksumStatus.VERIFIED,
                        cursor=None,
                        complete=True,
                    )
                ),
            ),
            ErrorCode.PERMIT_INVALID,
        )
        machine.accept_page(
            PullPage(
                context=sync_context,
                permit_id=permit_id,
                nonce=PERMIT_NONCE,
                snapshot_id="snapshot-1",
                snapshot_generation=7,
                page_index=0,
                page_bytes=1,
                checksum=ChecksumStatus.VERIFIED,
                cursor=None,
                complete=True,
            )
        )
        self.assertEqual(
            error_code(
                self,
                lambda: machine.commit(
                    CommitRequest(
                        context=sync_context,
                        permit_id=permit_id,
                        nonce="wrong-commit-nonce",
                        snapshot_id="snapshot-1",
                        snapshot_generation=7,
                    )
                ),
            ),
            ErrorCode.PERMIT_INVALID,
        )

        abort_machine, abort_context, _, abort_permit_id = ready_machine()
        abort_machine.open_pull(
            OpenPullRequest(abort_context, abort_permit_id, PERMIT_NONCE)
        )
        self.assertEqual(
            error_code(
                self,
                lambda: abort_machine.abort_pull(
                    PullAbortRequest(
                        context=abort_context,
                        permit_id=abort_permit_id,
                        nonce="wrong-abort-nonce",
                        reason=PullAbortReason.CANCELLED,
                    )
                ),
            ),
            ErrorCode.PERMIT_INVALID,
        )

    def test_override_requires_confirmation_expiry_and_single_nonce(self) -> None:
        machine, sync_context, selected_range, permit_id = ready_machine()
        baseline = machine.current_override_baseline()
        fence = quiesced_fence(machine, "fence-1")
        machine.prepare_override(
            prepare_request(
                sync_context,
                selected_range,
                baseline,
                fence,
                "override-1",
                "nonce-1",
                2_000,
            ),
            1_000,
        )
        self.assertEqual(
            error_code(
                self,
                lambda: machine.confirm_override(
                    confirm_request(sync_context, "override-1", "wrong-nonce", fence),
                    1_000,
                ),
            ),
            ErrorCode.OVERRIDE_TOKEN_INVALID,
        )
        self.assertEqual(machine.state().permit.permit_id, permit_id)
        authorization = machine.confirm_override(
            confirm_request(sync_context, "override-1", "nonce-1", fence),
            1_000,
        )
        self.assertEqual(authorization.abandoned_events, selected_range)
        self.assertEqual(authorization.baseline.snapshot_id, "snapshot-1")
        self.assertIsNone(machine.state().permit)
        self.assertEqual(machine.state().phase, SyncPhase.OVERRIDE_PENDING_EXECUTION)
        self.assertEqual(machine.next_action(), SyncAction.none())
        self.assertEqual(
            error_code(
                self,
                lambda: machine.confirm_override(
                    confirm_request(
                        sync_context,
                        authorization.token_id,
                        authorization.nonce,
                        authorization.fence,
                    ),
                    1_000,
                ),
            ),
            ErrorCode.OVERRIDE_ALREADY_USED,
        )

        expired, expired_context, expired_range, _ = ready_machine()
        expired_baseline = expired.current_override_baseline()
        expired_fence = quiesced_fence(expired, "fence-expired")
        expired.prepare_override(
            prepare_request(
                expired_context,
                expired_range,
                expired_baseline,
                expired_fence,
                "override-expired",
                "nonce-expired",
                2_000,
            ),
            1_000,
        )
        self.assertEqual(
            error_code(
                self,
                lambda: expired.confirm_override(
                    confirm_request(
                        expired_context,
                        "override-expired",
                        "nonce-expired",
                        expired_fence,
                    ),
                    2_000,
                ),
            ),
            ErrorCode.OVERRIDE_TOKEN_EXPIRED,
        )
        self.assertEqual(expired.state().phase, SyncPhase.PULL_READY)

    def test_expired_prepared_override_is_swept_and_new_token_can_prepare(self) -> None:
        invalid, invalid_context, invalid_range, _ = ready_machine()
        invalid_baseline = invalid.current_override_baseline()
        invalid_fence = quiesced_fence(invalid, "fence-expiry-invalid-request")
        self.assertEqual(
            error_code(
                self,
                lambda: invalid.prepare_override(
                    prepare_request(
                        invalid_context,
                        invalid_range,
                        invalid_baseline,
                        invalid_fence,
                        "override-expiry-invalid",
                        "nonce-expiry-invalid",
                        1_000,
                    ),
                    1_000,
                ),
            ),
            ErrorCode.OVERRIDE_TOKEN_EXPIRED,
        )
        self.assertEqual(invalid.state().override_tokens, [])

        machine, sync_context, selected_range, _ = ready_machine()
        baseline = machine.current_override_baseline()
        fence = quiesced_fence(machine, "fence-expiry-sweep")
        machine.prepare_override(
            prepare_request(
                sync_context,
                selected_range,
                baseline,
                fence,
                "override-sweep-old",
                "nonce-sweep-old",
                2_000,
            ),
            1_000,
        )
        self.assertEqual(
            error_code(
                self,
                lambda: machine.confirm_override(
                    confirm_request(
                        sync_context,
                        "override-sweep-old",
                        "nonce-sweep-old",
                        fence,
                    ),
                    2_000,
                ),
            ),
            ErrorCode.OVERRIDE_TOKEN_EXPIRED,
        )
        self.assertEqual(
            machine.state().override_tokens[0].status,
            OverrideTokenStatus.EXPIRED,
        )
        next_baseline = machine.current_override_baseline()
        next_fence = quiesced_fence(machine, "fence-expiry-sweep-next")
        machine.prepare_override(
            prepare_request(
                sync_context,
                selected_range,
                next_baseline,
                next_fence,
                "override-sweep-new",
                "nonce-sweep-new",
                3_000,
            ),
            1_000,
        )

    def test_override_rejects_unknown_concurrent_apply_and_changed_baseline(
        self,
    ) -> None:
        unknown, sync_context, selected_range, _ = ready_machine()
        unknown.record_anki_application(
            AnkiApplicationEvidence(
                context=sync_context,
                state=ApplicationState.UNKNOWN,
                applied_watermark=Watermark.unknown(),
            )
        )
        baseline = unknown.current_override_baseline()
        unknown_fence = quiesced_fence(unknown, "fence-unknown")
        self.assertEqual(
            error_code(
                self,
                lambda: unknown.prepare_override(
                    prepare_request(
                        sync_context,
                        selected_range,
                        baseline,
                        unknown_fence,
                        "override-unknown",
                        "nonce-unknown",
                        2_000,
                    ),
                    1_000,
                ),
            ),
            ErrorCode.CONCURRENT_APPLY_UNKNOWN,
        )
        assert_no_pull_or_commit(self, unknown)

        changed, changed_context, changed_range, _ = ready_machine()
        changed_baseline = changed.current_override_baseline()
        changed_fence = quiesced_fence(changed, "fence-changed")
        changed.prepare_override(
            prepare_request(
                changed_context,
                changed_range,
                changed_baseline,
                changed_fence,
                "override-changed",
                "nonce-changed",
                2_000,
            ),
            1_000,
        )
        changed.record_local_event(
            LocalEvent(
                context=SyncContext(
                    device_id=changed_context.device_id,
                    profile_id=changed_context.profile_id,
                    scope=changed_context.scope,
                    session_id=changed_context.session_id,
                    local_generation=2,
                ),
                sequence=changed_range.last_sequence + 1,
            )
        )
        self.assertEqual(changed.state().phase, SyncPhase.UPLOAD_REQUIRED)
        self.assertEqual(len(changed.state().override_tokens), 1)
        self.assertEqual(
            changed.state().override_tokens[0].status,
            OverrideTokenStatus.EXPIRED,
        )
        restored = SyncMachine.restore(
            PersistedState.from_dict(changed.state().to_dict())
        )
        self.assertEqual(restored.state(), changed.state())
        assert_no_pull_or_commit(self, changed)

        current, _, _ = applied_machine()
        forged_pending = current.into_state()
        forged_pending.phase = SyncPhase.OVERRIDE_PENDING_EXECUTION
        forged_pending.override_tokens = copy.deepcopy(changed.state().override_tokens)
        with self.assertRaises(SyncV2Error):
            SyncMachine.restore(forged_pending)

    def test_override_confirmation_does_not_delete_events_and_clears_old_download(
        self,
    ) -> None:
        machine, sync_context, selected_range, permit_id = ready_machine()
        baseline = machine.current_override_baseline()
        fence = quiesced_fence(machine, "fence-download")
        machine.prepare_override(
            prepare_request(
                sync_context,
                selected_range,
                baseline,
                fence,
                "override-download",
                "nonce-download",
                2_000,
            ),
            1_000,
        )
        machine.confirm_override(
            confirm_request(sync_context, "override-download", "nonce-download", fence),
            1_000,
        )
        self.assertEqual(machine.state().local_events, selected_range)
        self.assertEqual(len(machine.state().receipts), 2)
        self.assertIsNone(machine.state().snapshot)
        self.assertIsNone(machine.state().permit)
        self.assertEqual(machine.state().phase, SyncPhase.OVERRIDE_PENDING_EXECUTION)
        self.assertEqual(machine.next_action(), SyncAction.none())
        self.assertEqual(
            error_code(
                self,
                lambda: machine.commit(
                    CommitRequest(
                        context=sync_context,
                        permit_id=permit_id,
                        nonce=PERMIT_NONCE,
                        snapshot_id="snapshot-1",
                        snapshot_generation=7,
                    )
                ),
            ),
            ErrorCode.PULL_INCOMPLETE,
        )

    def test_override_quiesced_fence_allows_upload_failure_and_pending_durable_receipts(
        self,
    ) -> None:
        sync_context = context()
        selected_range = event_range(1, 2)
        failed_upload = SyncMachine()
        begin(failed_upload, sync_context, selected_range)
        failed_upload.record_upload(
            UploadReport(
                context=sync_context,
                range=selected_range,
                outcome=UploadOutcome.FAILED,
                checksum=ChecksumStatus.UNKNOWN,
                receipts=[],
            )
        )
        failed_baseline = failed_upload.current_override_baseline()
        failed_fence = quiesced_fence(failed_upload, "fence-upload-failed")
        failed_upload.prepare_override(
            prepare_request(
                sync_context,
                selected_range,
                failed_baseline,
                failed_fence,
                "override-upload-failed",
                "nonce-upload-failed",
                2_000,
            ),
            1_000,
        )
        failed_upload.confirm_override(
            confirm_request(
                sync_context,
                "override-upload-failed",
                "nonce-upload-failed",
                failed_fence,
            ),
            1_000,
        )
        self.assertEqual(
            failed_upload.state().phase,
            SyncPhase.OVERRIDE_PENDING_EXECUTION,
        )
        self.assertEqual(failed_upload.next_action(), SyncAction.none())

        durable_pending = SyncMachine()
        begin(durable_pending, sync_context, selected_range)
        upload(
            durable_pending,
            sync_context,
            selected_range,
            UploadOutcome.COMPLETE,
            ChecksumStatus.VERIFIED,
            ReceiptStatus.DURABLE_RECEIVED,
        )
        durable_baseline = durable_pending.current_override_baseline()
        durable_fence = quiesced_fence(durable_pending, "fence-durable-pending")
        durable_pending.prepare_override(
            prepare_request(
                sync_context,
                selected_range,
                durable_baseline,
                durable_fence,
                "override-durable-pending",
                "nonce-durable-pending",
                2_000,
            ),
            1_000,
        )
        durable_pending.confirm_override(
            confirm_request(
                sync_context,
                "override-durable-pending",
                "nonce-durable-pending",
                durable_fence,
            ),
            1_000,
        )
        self.assertEqual(
            durable_pending.state().phase,
            SyncPhase.OVERRIDE_PENDING_EXECUTION,
        )
        self.assertEqual(len(durable_pending.state().receipts), 2)
        self.assertEqual(durable_pending.next_action(), SyncAction.none())

    def test_override_rejects_partial_durable_receipt_coverage(self) -> None:
        sync_context = context()
        selected_range = event_range(1, 2)
        machine = SyncMachine()
        begin(machine, sync_context, selected_range)
        machine.record_upload(
            UploadReport(
                context=sync_context,
                range=selected_range,
                outcome=UploadOutcome.COMPLETE,
                checksum=ChecksumStatus.VERIFIED,
                receipts=receipts(
                    sync_context,
                    event_range(1, 1),
                    ReceiptStatus.DURABLE_RECEIVED,
                ),
            )
        )
        self.assertEqual(machine.state().phase, SyncPhase.RECONCILE_RECEIPTS)
        baseline = machine.current_override_baseline()
        fence = quiesced_fence(machine, "fence-partial-durable")
        self.assertEqual(
            error_code(
                self,
                lambda: machine.prepare_override(
                    prepare_request(
                        sync_context,
                        selected_range,
                        baseline,
                        fence,
                        "override-partial-durable",
                        "nonce-partial-durable",
                        2_000,
                    ),
                    1_000,
                ),
            ),
            ErrorCode.OVERRIDE_STATE_NOT_ELIGIBLE,
        )
        assert_no_pull_or_commit(self, machine)
        self.assertEqual(
            machine.next_action(),
            SyncAction.query_receipts(selected_range),
        )

    def test_override_rejects_applying_or_unknown_fence_and_receipt_changed_after_prepare(
        self,
    ) -> None:
        applying, sync_context, selected_range, _ = ready_machine()
        applying_baseline = applying.current_override_baseline()
        applying_fence = applying.current_apply_fence(
            "fence-applying", ApplyFenceStatus.APPLYING
        )
        self.assertEqual(
            error_code(
                self,
                lambda: applying.prepare_override(
                    prepare_request(
                        sync_context,
                        selected_range,
                        applying_baseline,
                        applying_fence,
                        "override-applying",
                        "nonce-applying",
                        2_000,
                    ),
                    1_000,
                ),
            ),
            ErrorCode.OVERRIDE_FENCE_NOT_QUIESCED,
        )
        applying_fence.status = ApplyFenceStatus.UNKNOWN
        unknown_baseline = applying.current_override_baseline()
        self.assertEqual(
            error_code(
                self,
                lambda: applying.prepare_override(
                    prepare_request(
                        sync_context,
                        selected_range,
                        unknown_baseline,
                        applying_fence,
                        "override-unknown-fence",
                        "nonce-unknown-fence",
                        2_000,
                    ),
                    1_000,
                ),
            ),
            ErrorCode.OVERRIDE_FENCE_NOT_QUIESCED,
        )

        changed_receipt = SyncMachine()
        begin(changed_receipt, sync_context, selected_range)
        upload(
            changed_receipt,
            sync_context,
            selected_range,
            UploadOutcome.COMPLETE,
            ChecksumStatus.VERIFIED,
            ReceiptStatus.DURABLE_RECEIVED,
        )
        baseline = changed_receipt.current_override_baseline()
        fence = quiesced_fence(changed_receipt, "fence-receipt-change")
        changed_receipt.prepare_override(
            prepare_request(
                sync_context,
                selected_range,
                baseline,
                fence,
                "override-receipt-change",
                "nonce-receipt-change",
                2_000,
            ),
            1_000,
        )
        changed_receipt.record_receipts(
            ReceiptQueryReport(
                context=sync_context,
                range=selected_range,
                outcome=UploadOutcome.COMPLETE,
                receipts=receipts(
                    sync_context,
                    selected_range,
                    ReceiptStatus.APPLIED,
                ),
            )
        )
        self.assertEqual(
            error_code(
                self,
                lambda: changed_receipt.confirm_override(
                    confirm_request(
                        sync_context,
                        "override-receipt-change",
                        "nonce-receipt-change",
                        fence,
                    ),
                    1_000,
                ),
            ),
            ErrorCode.OVERRIDE_TOKEN_EXPIRED,
        )
        self.assertEqual(changed_receipt.state().phase, SyncPhase.PENDING_APPLY)
        assert_no_pull_or_commit(self, changed_receipt)

    def test_sync_05_cancel_or_timeout_after_partial_page_never_commits(self) -> None:
        machine, sync_context, _, permit_id = ready_machine()
        machine.open_pull(OpenPullRequest(sync_context, permit_id, PERMIT_NONCE))
        machine.accept_page(
            PullPage(
                context=sync_context,
                permit_id=permit_id,
                nonce=PERMIT_NONCE,
                snapshot_id="snapshot-1",
                snapshot_generation=7,
                page_index=0,
                page_bytes=32,
                checksum=ChecksumStatus.VERIFIED,
                cursor="cursor-next",
                complete=False,
            )
        )
        machine.abort_pull(
            PullAbortRequest(
                context=sync_context,
                permit_id=permit_id,
                nonce=PERMIT_NONCE,
                reason=PullAbortReason.TIMED_OUT,
            )
        )
        self.assertEqual(machine.state().phase, SyncPhase.PULL_READY)
        self.assertEqual(machine.next_action().kind, "open_pull")
        self.assertEqual(
            error_code(
                self,
                lambda: machine.commit(
                    CommitRequest(
                        context=sync_context,
                        permit_id=permit_id,
                        nonce=PERMIT_NONCE,
                        snapshot_id="snapshot-1",
                        snapshot_generation=7,
                    )
                ),
            ),
            ErrorCode.PULL_INCOMPLETE,
        )


if __name__ == "__main__":
    unittest.main()
