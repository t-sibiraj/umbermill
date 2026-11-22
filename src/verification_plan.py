"""Spend bounded selector CPU on real patch replays before scheduling the remaining tests."""

import atexit
import importlib
import json
import math
from pathlib import Path
import tempfile
import time

from .patches import UnsupportedPatch, parse_patch
from .probe_worker import cpu
from .scheduler import evaluate_schedule, schedule_tests
from .verification_process import Processes, prepare_tree


def packed_bound(total, longest, count):
    if not count:
        return 0.0
    return min(2 * workers + max(longest, total / workers) for workers in range(1, min(8, count) + 1))


def verdicts(events, numbers):
    """A failed prefix is evidence. A pass requires a consistent, complete transcript."""
    order = tuple(numbers)
    expected = set(order)
    outcomes = {}
    failures = set()
    same_order = True
    active = None
    if not events or events[0] != ['ready']:
        return None, failures
    for position, event in enumerate(events[1:],start=1):
        kind, *fields = event
        if kind == 'complete':
            valid = (same_order and fields == [int(bool(failures))] and position == len(events)-1
                     and active is None and set(outcomes) == expected)
            return (outcomes if valid else None), failures
        if not fields or type(fields[0]) is not int or fields[0] not in expected:
            break
        n = fields[0]
        if kind == 'start' and len(fields) == 1 and active is None and n not in outcomes:
            active = n
            same_order &= len(outcomes) < len(order) and order[len(outcomes)] == n
        elif active != n:
            break
        elif kind == 'failure' and len(fields) == 2 and fields[1] in ('setup','call','teardown'):
            failures.add(n)
        elif kind == 'state' and len(fields) == 1:
            continue
        elif kind == 'end' and len(fields) == 3:
            _, answer, cost = fields
            if (answer not in ('pass','unknown','partial','failed') or (answer == 'failed') != (n in failures)
                    or type(cost) not in (int,float) or not math.isfinite(cost) or cost < 0):
                break
            outcomes[n] = answer, cost
            active = None
        else:
            break
    return None, failures


def choose_work(selected, candidates, witnesses, held, duration, estimate, budget, start_cost):
    """Compare a run with leaving the same work in the packed schedule."""
    ranked = sorted(candidates, key=lambda n: (n not in witnesses, -duration[n] / estimate[n], estimate[n], n))
    order = []
    spend = start_cost
    for n in ranked:
        if spend + estimate[n] <= budget:
            order.append(n)
            spend += estimate[n]
    if not order:
        return []
    fixed = selected.difference(order)
    # Held long tests can only leave the schedule with affirmative peer evidence.
    planned = set(order)
    possible = {n for n in held if held[n] <= planned}
    fixed = fixed - possible
    durations = [duration[n] for n in order]
    longest = [max((duration[n] for n in fixed), default=0.0)]
    for value in reversed(durations):
        longest.append(max(longest[-1], value))
    longest.reverse()
    count = len(fixed) + len(order)
    seconds = sum(duration[n] for n in fixed) + sum(durations)
    original = packed_bound(sum(duration[n] for n in selected), max(duration[n] for n in selected), len(selected))
    best = original
    cutoff = 0
    checked = set()
    charge = start_cost
    for index, n in enumerate(order, start=1):
        checked.add(n)
        count -= 1
        seconds -= duration[n]
        charge += estimate[n]
        uncertain = [duration[k] for k in possible if not held[k] <= checked]
        prediction = packed_bound(max(0.0,seconds) + sum(uncertain), max(longest[index], max(uncertain, default=0.0)), count + len(uncertain)) + charge
        if prediction < best:
            best, cutoff = prediction, index
    return order[:cutoff]


class VerificationPlan:
    def __init__(self, checks):
        self.checks = checks
        self.nodes = checks.nodeids
        self.durations = checks.durations
        self.history = {}
        self.spent = 0.0
        self.failures = set()
        self.inferred = set()
        self.collection = None
        self.order = {}
        self.startup = 0.55
        self.groups = {n: name.rsplit("::",1)[0] for n,name in self.nodes.items()}
        self.processes = Processes(checks.root)
        atexit.register(self.processes.close)

    def _collection(self, selector):
        if self.collection is None:
            pristine = json.loads((selector._maps_path.parent / 'pristine-collection.json').read_bytes())
            nodes = {node:n for n,node in self.nodes.items()}
            files = {node.split('::',1)[0] for node in nodes}
            sequence = [node for node in pristine['items'] if node.split('::',1)[0] in files]
            if (pristine.get('state') != 'collected' or pristine.get('code') != 0 or pristine.get('errors')
                    or len(sequence) != len(set(sequence)) or not nodes.keys() <= set(sequence)):
                self.collection = ()
            else:
                self.collection = tuple(sequence)
                self.order = {nodes[node]:i for i,node in enumerate(sequence) if node in nodes}
        return self.collection

    def _saving(self, remaining, remove):
        original = schedule_tests(remaining,self.durations)
        original_cost = evaluate_schedule(original,self.durations)
        repacked = schedule_tests(remaining-remove,self.durations)
        return original_cost - evaluate_schedule(repacked,self.durations)

    def _held(self, selected, spendable, selector, estimates, external):
        # A cheap passing neighbour is evidence about the class or SQL fixture it actually uses.
        # A single method can still fail independently of its peers; this is a risk policy.
        long = {n for n in spendable if self.durations[n] > 4.0 and estimates[n] > 0.8}
        if not long:
            return {}
        fixture_masks = [mask for rel,mask in selector._reads.items() if rel.startswith("tests/fixtures/") and rel.endswith(".sql")]
        result = {}
        for n in long:
            relatives = set()
            family = self.groups[n]
            related_fixtures = [m for m in fixture_masks if (m >> n) & 1]
            for k in selected | external:
                if k != n and (self.groups[k] == family or any((m >> k) & 1 for m in related_fixtures)):
                    relatives.add(k)
            peers = sorted((relatives & (spendable | external)) - long, key=lambda k:(k not in external, estimates.get(k,0.0), k))
            # No extrapolation across a class or fixture without two measured witnesses.
            if len(peers) >= 2:
                result[n] = (set(peers[:3]), relatives)
        return result

    def _stage(self, selector, available, remaining, paths, patch, estimates, budget):
        tops = sorted(available, key=lambda n:(-self.durations[n],n))[:20]
        options = {}
        for name in ('executor_stage','token_replay','verification_units'):
            try:
                component = importlib.import_module('.'+name, __package__)
            except ModuleNotFoundError as error:
                if error.name != __package__+'.'+name:
                    raise
                continue
            if name == 'executor_stage':
                accepted = component.stage_candidates(self.checks.root,patch,[self.nodes[n] for n in tops])
                reverse = {self.nodes[n]:n for n in tops}
                costs = component.method_costs()
                for node in accepted:
                    if node in reverse:
                        options[reverse[node]] = (False, self.startup + costs[node])
            else:
                for n in tops:
                    if self.durations[n] <= .3:
                        continue
                    if name == 'verification_units':
                        if any(p.startswith('tests/fixtures/') and not (selector._reads.get(p,0) >> n) & 1 for p in paths):
                            continue
                        if component.eligible(self.nodes[n],paths,selector._maps_path.parent):
                            # A shortened success requires a new, complete, cold run.
                            options.setdefault(n,(True, 2*self.startup+estimates[n]))
                    elif component.eligible(self.nodes[n],paths):
                        options[n] = (False, self.startup + min(.6,estimates[n]))
        best = None
        for n,(partial,cost) in options.items():
            if cost >= budget:
                continue
            saving = self._saving(remaining,{n})
            reward = saving-cost
            if reward > 0 and (best is None or reward > best[0]):
                best = reward,n,partial,min(budget,saving)
        return best

    def omit(self, patch, selection, selector, already_omitted=(), must=(), *, allow_inferred=False):
        """Return confirmed passes. Optional peer-based omissions are recorded separately."""
        c = self.checks
        self.failures.clear()
        self.inferred.clear()
        if (not c.usable or selection.basis == 'shared state' or time.monotonic() >= c.deadline
                or c.worker_cpu >= c.budget or not selection.tests):
            return set()
        started = cpu()
        try:
            return self._evaluate(patch,selection,selector,set(already_omitted),set(must),started,allow_inferred)
        except (OSError,ValueError,TypeError,KeyError,AttributeError,RecursionError,UnsupportedPatch):
            return set()
        finally:
            charged = max(0.0,cpu()-started)
            self.spent += charged
            c.worker_cpu += charged

    def _evaluate(self, patch, selection, selector, external, pinned, started, allow_inferred):
        native_only = selection.basis == "layout"
        allow_inferred = allow_inferred and not native_only
        changes = parse_patch(patch)
        paths = tuple(ch.old_path for ch in changes)
        if (len(paths) > 12 or any(ch.added or ch.deleted or ch.old_path != ch.new_path or
                not ch.old_path.startswith(('sqlglot/','tests/')) for ch in changes)):
            return set()
        tests = set(selection.tests) - external
        if not tests:
            return set()
        guard = selector._unmapped | getattr(selector._reader_index,'guard',0)
        direct = 0
        for path in paths:
            if path.startswith('tests/'):
                direct |= selector._tests_by_file.get(path,0) | selector._reads.get(path,0)
        # Direct test or fixture readers may be replayed, but must never be removed by analogy.
        eligible = {n for n in tests-pinned if not (guard >> n) & 1}
        if not eligible:
            return set()
        total = sum(self.durations[n] for n in tests)
        largest = max(self.durations[n] for n in tests)
        plan_cost = packed_bound(total,largest,len(tests))
        budget = min(self.checks.budget-self.checks.worker_cpu, max(1.0,min(5.5,0.55*plan_cost)))
        if budget <= self.startup:
            return set()
        estimates = {n:self.history.get(n, max(.003,self.durations[n])) for n in eligible}
        held = self._held(tests,eligible,selector,estimates,external) if allow_inferred else {}
        held = {n:v for n,v in held.items() if not (direct >> n) & 1 and not ((v[1] & pinned))}
        requirements = {n:v[0]-external for n,v in held.items()}
        witnesses = set().union(*requirements.values()) if requirements else set()
        cheap = {n for n in eligible if estimates[n] < max(.75,.7*(2+self.durations[n]))}
        candidates = cheap - set(held)
        regular = choose_work(tests,candidates,witnesses,requirements,self.durations,estimates,budget,self.startup)
        if regular:
            best_case = set(regular)
            best_case.update(n for n, required in requirements.items() if required <= best_case)
            if self._saving(tests,best_case) <= self.startup + sum(estimates[n] for n in regular):
                regular = []
        staged = None if native_only else self._stage(selector,eligible,tests,paths,patch,estimates,budget)
        if (not regular and not staged) or not self._collection(selector):
            return set()
        observed = {}
        confirmed_failures = self.failures
        unsafe = False
        incomplete = False
        first = True
        with tempfile.TemporaryDirectory(prefix='umbermill-verify-') as temporary:
            scratch = Path(temporary)
            serial = 0
            def run(numbers, deferred, units, allowance):
                nonlocal serial, unsafe, incomplete, first
                group_start = cpu()
                remaining = budget-(cpu()-started)
                if remaining < .25 or time.monotonic()+.20 >= self.checks.deadline:
                    incomplete = True
                    return 'budget', None
                # The first group gets the warm checkpoint when its imports can be
                # activated. A declined activation always starts an unmodified interpreter.
                warm = (not native_only and first and all(p.startswith('sqlglot/') and p.endswith('.py') for p in paths)
                        and self.processes.hot())
                first = False
                for hot in ([True,False] if warm else [False]):
                    serial += 1
                    tree = scratch/f'source-{serial}'
                    if not prepare_tree(self.checks.root,tree,changes):
                        incomplete = True
                        return 'uncertain',None
                    order = sorted(numbers,key=self.order.__getitem__)
                    job = dict(tree=str(tree),base=str(self.checks.root),changes=paths,patch=patch,
                               native_only=native_only,
                               collection=self.collection,maps=str(selector._maps_path.parent),
                               members=[(n,self.nodes[n]) for n in order],after_failure=sorted(deferred),units=units)
                    limits = {n:min(remaining,max(.40,3*estimates[n]+.25)) for n in numbers}
                    before = cpu()
                    available = min(budget-(before-started),allowance-(before-group_start))
                    if available < .15 or time.monotonic()+.15 >= self.checks.deadline:
                        incomplete = True
                        return 'budget',None
                    status, stopped, events = self.processes.run(job,scratch/f'batch-{serial}.job',limits,
                        available,self.checks.deadline,hot)
                    unsafe |= any(row[0] in ('state','unattributed_failure') for row in events)
                    recorded, negatives = verdicts(events, order)
                    confirmed_failures.update(negatives)
                    # Only an unfinished hot attempt can be retried on a new,
                    # independently materialized cold tree. Keep its negative evidence.
                    if not hot or status not in ('fresh','uncertain') or unsafe:
                        break
                observed.update((n,'failed') for n in confirmed_failures)
                if status == 'complete':
                    if recorded is None:
                        incomplete = True
                        unsafe = True
                        return 'uncertain', None
                    self.startup = max(.15, .8*self.startup+.2*max(0.,cpu()-before-sum(v[1] for v in recorded.values())))
                    for n,(answer,cost) in recorded.items():
                        observed[n] = 'failed' if n in confirmed_failures else answer
                        if answer in ('pass','failed'):
                            self.history[n] = max(.002, (estimates[n]+cost)*.5)
                else:
                    incomplete = True
                    if stopped is not None and stopped not in confirmed_failures:
                        observed[stopped] = 'stopped'
                return status,stopped

            if staged:
                _,n,partial,allowance = staged
                run([n],set(),partial,allowance)
            if unsafe:
                return set()
            rest = [n for n in regular if n not in observed]
            if rest:
                deferred = {n for n in rest if n not in witnesses and self.durations[n] < .06}
                status,stopped = run(rest,deferred,False,budget)
                if status == 'stop' and stopped is not None and not unsafe:
                    # An interrupted child gives no passing receipts. Retain the suspect.
                    rest = [n for n in rest if n != stopped and n not in confirmed_failures]
                    pending = tests - {n for n,v in observed.items() if v=='pass'}
                    ceiling = self._saving(pending,set(rest)) if rest else 0
                    if ceiling > self.startup+sum(estimates[n] for n in rest):
                        run(rest,set(),False,ceiling)
            if unsafe:
                return set()
            deferred = {n for n,v in observed.items() if v in ('unknown','partial')}
            if deferred:
                left = budget-(cpu()-started)
                pending = tests - {n for n,v in observed.items() if v=='pass'}
                rerun = choose_work(pending,deferred,set(),{},self.durations,estimates,left,self.startup)
                ceiling = self._saving(pending,set(rerun)) if rerun else 0
                if ceiling > self.startup+sum(estimates[n] for n in rerun):
                    run(rerun,set(),False,ceiling)
        if unsafe:
            return set()
        passes = {n for n,v in observed.items() if v=='pass'}
        for n,v in observed.items():
            if v == 'stopped' and self.checks.uses_pool(self.nodes[n]):
                passes.difference_update(k for k in tuple(passes) if k > n and self.groups[k] == self.groups[n])
        adverse = {n for n,v in observed.items() if v in ('failed','stopped')}
        uncertain = any(v in ('unknown','partial','stopped') for v in observed.values())
        if passes and not incomplete and not uncertain and len(adverse | pinned) < 4:
            evidence = passes | external
            for n,(required,related) in held.items():
                if n not in observed and required <= evidence and not related & (adverse|pinned):
                    self.inferred.add(n)
        return passes
