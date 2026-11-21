"""Compose Contest features and advance external work outside database transactions."""
from time import monotonic

from engine.replay import VerificationError

from .arena import RUN_QUERY_BATCH_SIZE
from .database import Database
from .duels import DuelService
from .executions import ExecutionService, review_archive
from .preliminary import Preliminary


PRELIMINARY_POLL_INTERVAL = 10


class Contest:
    """One shared database, with explicit feature services for every caller."""

    def __init__(self, path, pools=None):
        self.database = Database(path)
        self.maps = pools or {}
        self.duels = DuelService(self.database, self.maps)
        self.runs = ExecutionService(self.database)
        self.preliminary = Preliminary(self.database, self.maps)


class ContestWorker:
    def __init__(self, contest, archive_root, arena):
        self.contest, self.archive_root = contest, archive_root
        self.arena = arena

    def submit_next(self):
        request = self.contest.runs.claim()
        if request:
            status = self.arena.submit(request)
            self.contest.runs.complete_submission(request['run_id'], status)
            return True
        return False

    def poll_next(self):
        claim = self.contest.runs.claim_poll()
        if claim is None:
            return False
        status, payload = self.arena.lookup(claim.run_id)
        self.contest.runs.complete_poll(claim, status, payload)
        return True

    def poll_preliminary_round(self, stop=None):
        targets = self.contest.runs.preliminary_poll_targets()
        for offset in range(0, len(targets), RUN_QUERY_BATCH_SIZE):
            if stop is not None and stop.is_set():
                return
            batch = targets[offset:offset + RUN_QUERY_BATCH_SIZE]
            claims = self.contest.runs.claim_preliminary_polls(batch)
            if claims:
                status, payload = self.arena.lookup_batch([claim.run_id for claim in claims])
                self.contest.runs.complete_preliminary_polls(claims, status, payload)

    def poll_preliminary(self, stop):
        next_round = monotonic()
        while not stop.wait(max(0, next_round - monotonic())):
            self.poll_preliminary_round(stop)
            # Advance to the next scheduled tick, skipping ticks consumed by this round.
            elapsed = monotonic() - next_round
            intervals = int(elapsed // PRELIMINARY_POLL_INTERVAL) + 1
            next_round += intervals * PRELIMINARY_POLL_INTERVAL

    def review_next(self):
        pending = self.contest.runs.next_review()
        if pending is None:
            return False
        request, arena_result, ladder = pending
        run_id, state = request['run_id'], arena_result['state']
        if state == 'platform_error':
            self.contest.runs.complete_review(run_id, 'void', issue=arena_result['reason'])
            return True
        try:
            manifest, issue = review_archive(self.archive_root, request, state, ladder=ladder)
        except VerificationError as error:
            self.contest.runs.complete_review(run_id, 'held', issue=str(error))
        else:
            review = 'held' if issue else ('effective' if state == 'finished' else 'build_failed')
            self.contest.runs.complete_review(run_id, review, manifest=manifest, issue=issue)
        return True
