"""Fast, still fully isolated MIXIE smoke suite. Use full run_isolated.py for release."""
from pathlib import Path
import subprocess
import sys

# High-signal tests from distinct subsystems. Full suite remains unchanged.
QUICK_MODULES = (
    "test_receipt_queue_refresh_20261123",
    "test_review_contract_v10i",
    "test_finalization_integration_stabilization.FinalizationIntegrationTests.test_auto_ingest_archives_both_copies_then_cleans_complete_delivery",
    "test_finalization_integration_stabilization.FinalizationIntegrationTests.test_auto_ingest_archive_failure_never_removes_originals",
    "test_finalization_integration_stabilization.FinalizationIntegrationTests.test_auto_ingest_shared_incomplete_delivery_keeps_both_sources",
    "test_finalization_integration_stabilization.FinalizationIntegrationTests.test_main_archive_failure_keeps_queue_then_retries_without_second_import",
    "test_completion.CompletionTests.test_missing_key_does_not_start_automatic_cleanup",
    "test_review_evidence_20261123",
    "test_album_identity_output.AlbumIdentityOutputTests.test_unknown_album_verified_single_receives_song_single_title",
    "test_album_identity_output.AlbumIdentityOutputTests.test_one_track_album_type_album_not_mislabeled_single",
    "test_active_queue_and_totals_20261123",
    "test_stress_log_housekeeping_20261123",
    "test_shared_archive_batch_performance_20261123",
    "test_project_layout",
    "test_runner_stabilization",
    "test_ingest_safety",
    "test_archive_safety",
    "test_shared_original_pin_recovery",
    "test_queue_workflow",
    "test_pending_auto_retry",
    "test_standard_us_edition",
    "test_catalog_track_count",
    "test_finish_import_v10i",
    "test_album_identity_output.AlbumIdentityOutputTests.test_back_from_dead_all_tracks_ambiguous_commit_canonical_mp3_and_flac",
    "test_album_identity_output.AlbumIdentityOutputTests.test_strange_case_all_tracks_ambiguous_keep_explicit_evidence_in_final_outputs",
    "test_album_identity_output.AlbumIdentityOutputTests.test_witch_verified_featured_band_member_reaches_actual_output",
    "test_album_identity_output.AlbumIdentityOutputTests.test_joint_halestorm_i_prevail_single_keeps_shared_lead_artist_automatically",
    "test_album_identity_stabilization.AlbumIdentityStabilizationTests.test_americana_album_context_recovers_weak_hits_and_unique_unmatched_pay_the_man",
    "test_album_identity_stabilization.AlbumIdentityStabilizationTests.test_americana_context_retains_real_live_version_conflict",
    "test_album_identity_output.AlbumIdentityOutputTests.test_americana_two_pass_weak_fingerprints_and_no_hit_commit_with_shuffled_numbers",
    "test_album_identity_output.AlbumIdentityOutputTests.test_live_competitors_require_review_clean_explicit_catalogs_can_import",
)

if __name__ == "__main__":
    runner = Path(__file__).with_name("run_isolated.py")
    command = [sys.executable, "-I", "-B", str(runner), *QUICK_MODULES]
    raise SystemExit(subprocess.run(command, check=False).returncode)
