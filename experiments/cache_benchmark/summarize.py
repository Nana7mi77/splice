"""Derive descriptive statistics and correctness checks from benchmark evidence.

Uses only the standard library and never changes the input evidence. This is an
exploratory paired benchmark: no significance, percentile, or production-hit-rate
claim is inferred from four short pairs and two complete-request pairs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics


MODES = ('no_prefix_reuse', 'prefix_hit')
SHORT_RESERVES = (15, 20, 50, 16)
FULL_RESERVES = (15, 20)


def distribution(values):
    values = list(values)
    return {'count': len(values), 'median': statistics.median(values),
            'min': min(values), 'max': max(values)} if values else None


def amortization(build_seconds, baseline_seconds, hit_seconds):
    """Compare N*B with C+N*H; strictly faster requires N > C/(B-H)."""
    savings = baseline_seconds - hit_seconds
    return {
        'build_seconds': build_seconds,
        'baseline_seconds': baseline_seconds,
        'hit_seconds': hit_seconds,
        'saved_seconds_per_hit': savings,
        'strictly_faster_min_requests': math.floor(build_seconds / savings) + 1 if savings > 0 else None,
        'modeled_first_request_seconds': build_seconds + hit_seconds,
        'first_request_is_measured': False,
    }


def normalized(value):
    """Ignore only a fixture draft UUID, preserving capability and resource IDs."""
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()
                if not (key == 'id' and value.get('status') == 'draft')}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


def gate_list(value):
    return sorted(value, key=lambda item: json.dumps(item, sort_keys=True))


def semantic_signature(correctness):
    if not correctness or correctness.get('parse_status') != 'parsed':
        return None
    cases = correctness.get('cases', {})
    if set(cases) != {'default', 'insufficient_cash', 'missing_finance_permission'}:
        return None
    return {
        'facts': correctness['facts'],
        'gates': gate_list(correctness['gates']),
        'requested_outputs': sorted(correctness['requested_outputs']),
        'cases': {
            name: {
                'status': case['status'], 'error_type': (case.get('error') or {}).get('type'),
                'draft_count': case['draft_count'], 'plan': case.get('plan'),
                'facts': normalized(case.get('facts')), 'outputs': normalized(case['outputs']),
                'gate_checks': case.get('gate_checks'),
            }
            for name, case in cases.items()
        },
    }


def correctness_summary(row):
    correctness = row.get('correctness')
    if not correctness:
        return {'available': False, 'expected_checks_passed': False}
    reserve = row['reserve']
    expected_facts = {
        'meal.query': '盖饭', 'meal.taste': '清淡', 'spending.max_cny': 30,
        'budget.month': '2026-10', 'budget.min_remaining_cny': reserve,
    }
    actual_facts = correctness.get('facts', {})
    request_facts_match = all(actual_facts.get(key) == value for key, value in expected_facts.items())
    expected_outputs = {'order.draft', 'budget.assessment', 'budget.reserve_preserved'}
    requested_outputs = correctness.get('requested_outputs', [])
    gates = correctness.get('gates', [])
    explicit_checks = {
        'requested_outputs_exactly_expected_three': len(requested_outputs) == 3 and set(requested_outputs) == expected_outputs,
        'gates_exactly_both_true': len(gates) == 2
            and {gate.get('fact') for gate in gates} == {'budget.within_limits', 'budget.reserve_preserved'}
            and all(gate.get('equals') is True for gate in gates),
    }
    checks = {}
    for name in ('default', 'insufficient_cash', 'missing_finance_permission'):
        case = correctness.get('cases', {}).get(name)
        expected_status = 'completed' if name == 'default' and reserve == 15 else 'blocked'
        expected_drafts = 1 if expected_status == 'completed' else 0
        expected_error = (None if expected_status == 'completed' else
                          'PermissionDenied' if name == 'missing_finance_permission' else 'GateRejected')
        observed_error = (case.get('error') or {}).get('type') if case else None
        outputs = case.get('outputs', {}) if case else {}
        assessment = outputs.get('budget.assessment', {})
        evidence_checks = {}
        if name == 'default':
            expected_assessment = {
                'planned_amount_cny': 24, 'projected_remaining_cny': 16,
                'cash_available_cny': 4200, 'projected_cash_cny': 4176,
            }
            evidence_checks = {
                'amounts_match_fixtures': all(assessment.get(key) == value for key, value in expected_assessment.items()),
                'within_budget_true': assessment.get('within_budget') is True,
                'within_cash_true': assessment.get('within_cash') is True,
                'reserve_boolean_matches_threshold': outputs.get('budget.reserve_preserved') is (reserve <= 16),
                'draft_total_24_if_success_or_absent_when_blocked': (
                    outputs.get('order.draft', {}).get('total_price_cny') == 24
                    if expected_status == 'completed' else 'order.draft' not in outputs
                ),
            }
        elif name == 'insufficient_cash':
            evidence_checks = {
                'cash_available_zero': assessment.get('cash_available_cny') == 0,
                'projected_cash_minus_24': assessment.get('projected_cash_cny') == -24,
                'within_cash_false': assessment.get('within_cash') is False,
                'draft_output_absent': 'order.draft' not in outputs,
            }
        checks[name] = {
            'status': case.get('status') if case else None,
            'error': case.get('error') if case else None,
            'draft_count': case.get('draft_count') if case else None,
            'expected_status': expected_status,
            'expected_error_type': expected_error,
            'expected_draft_count': expected_drafts,
            'evidence_checks': evidence_checks,
            'observed_budget_assessment': assessment or None,
            'observed_reserve_preserved': outputs.get('budget.reserve_preserved'),
            'observed_draft_total_cny': outputs.get('order.draft', {}).get('total_price_cny'),
            'passed': bool(case and case.get('status') == expected_status
                           and case.get('draft_count') == expected_drafts
                           and observed_error == expected_error and all(evidence_checks.values())),
        }
    return {
        'available': True, 'parse_status': correctness.get('parse_status'),
        'facts': correctness.get('facts'), 'gates': correctness.get('gates'),
        'requested_outputs': correctness.get('requested_outputs'),
        'expected_request_facts': expected_facts,
        'request_facts_match': request_facts_match,
        'explicit_constraint_checks': explicit_checks,
        'format_corrections': correctness.get('format_corrections', []),
        'cases': checks,
        'expected_checks_passed': bool(correctness.get('parse_status') == 'parsed' and request_facts_match
                                       and all(explicit_checks.values())
                                       and all(case['passed'] for case in checks.values())),
    }


def row_summary(row):
    fields = ('name', 'reserve', 'mode', 'elapsed_seconds', 'first_token_seconds',
              'generated_tokens', 'first_forward_seconds', 'first_forward_input_tokens',
              'decode_forward_seconds', 'decode_forward_count', 'mean_decode_forward_seconds',
              'ended_with_eos')
    result = {field: row[field] for field in fields}
    if row.get('cache'):
        result['clone_seconds'] = row['cache']['clone_seconds']
    if row['full_completion']:
        result['correctness'] = correctness_summary(row)
    return result


def paired_comparison(a, b, build):
    ai, bi = a['output_token_ids'], b['output_token_ids']
    first_difference = next((i for i, (x, y) in enumerate(zip(ai, bi)) if x != y), None)
    if first_difference is None and len(ai) != len(bi):
        first_difference = min(len(ai), len(bi))
    result = {
        'reserve': a['reserve'], 'baseline': row_summary(a), 'hit': row_summary(b),
        'exact_token_match': ai == bi,
        'first_differing_token_index': first_difference,
        'same_generated_length': len(ai) == len(bi),
        'elapsed_seconds_saved': a['elapsed_seconds'] - b['elapsed_seconds'],
        'elapsed_reduction_fraction': 1 - b['elapsed_seconds'] / a['elapsed_seconds'],
        'elapsed_speedup_ratio': a['elapsed_seconds'] / b['elapsed_seconds'],
        'first_token_seconds_saved': a['first_token_seconds'] - b['first_token_seconds'],
        'first_token_reduction_fraction': 1 - b['first_token_seconds'] / a['first_token_seconds'],
        'first_token_speedup_ratio': a['first_token_seconds'] / b['first_token_seconds'],
    }
    if a['full_completion']:
        sa, sb = semantic_signature(a.get('correctness')), semantic_signature(b.get('correctness'))
        result['semantic_comparison_available'] = sa is not None and sb is not None
        result['semantic_match'] = sa is not None and sb is not None and sa == sb
        result['semantic_fields_compared'] = [
            'parsed facts', 'gates', 'requested_outputs',
            'each Runtime case: status/error type/draft count/plan/computed facts/outputs/gate checks',
        ]
        result['semantic_fields_ignored'] = ['goal wording', 'fixture draft UUID', 'trace/audit record detail']
        result['amortization_pure_build'] = (
            amortization(build['build_seconds'], a['elapsed_seconds'], b['elapsed_seconds'])
            if len(ai) == len(bi) else None
        )
        result['amortization_eager_initialization'] = (
            amortization(build['lookup_build_clone_seconds'], a['elapsed_seconds'], b['elapsed_seconds'])
            if len(ai) == len(bi) else None
        )
        if len(ai) != len(bi):
            result['amortization_omitted_reason'] = 'Generated lengths differ; do not attribute all time difference to prefix reuse.'
    return result


def summarize(report):
    runs = report.get('runs', [])
    build = report.get('cache_initialization')
    by_key = {(row['full_completion'], row['reserve'], row['mode']): row for row in runs}
    missing = [dict(full_completion=full, reserve=reserve, mode=mode)
               for full, reserves in ((False, SHORT_RESERVES), (True, FULL_RESERVES))
               for reserve in reserves for mode in MODES if (full, reserve, mode) not in by_key]
    duplicate_keys = len(by_key) != len(runs)
    result = {
        'status': 'incomplete', 'source_status': report.get('status'),
        'scope': report.get('scope'), 'cloud_calls': report.get('cloud_calls'),
        'model': report.get('model'), 'identity': report.get('identity'),
        'environment': report.get('environment'), 'startup': report.get('startup'),
        'warmup_seconds': report.get('warmup_seconds'),
        'sample_scope': 'Four 16-token paired trials; two complete-request pairs. Descriptive, not a significance or production-hit-rate estimate.',
        'missing_runs': missing, 'duplicate_run_keys': duplicate_keys,
        'requests': {key: {field: value[field] for field in ('prompt_tokens', 'prefix_tokens', 'suffix_tokens')}
                     for key, value in report.get('requests', {}).items()},
        'cache_initialization': build,
        'short_pairs': [], 'full_pairs': [], 'short_group_statistics': {},
    }
    if not build:
        return result
    for full, reserves in ((False, SHORT_RESERVES), (True, FULL_RESERVES)):
        for reserve in reserves:
            a = by_key.get((full, reserve, MODES[0]))
            b = by_key.get((full, reserve, MODES[1]))
            if a and b:
                result['full_pairs' if full else 'short_pairs'].append(paired_comparison(a, b, build))
    for mode in MODES:
        short = [row for row in runs if not row['full_completion'] and row['mode'] == mode]
        result['short_group_statistics'][mode] = {
            'elapsed_seconds': distribution(row['elapsed_seconds'] for row in short),
            'first_token_seconds': distribution(row['first_token_seconds'] for row in short),
        }
    result['short_paired_statistics'] = {
        field: distribution(pair[field] for pair in result['short_pairs'])
        for field in ('elapsed_seconds_saved', 'elapsed_reduction_fraction', 'elapsed_speedup_ratio',
                      'first_token_seconds_saved', 'first_token_reduction_fraction', 'first_token_speedup_ratio')
    }
    groups = result['short_group_statistics']
    if all(groups[mode]['elapsed_seconds'] for mode in MODES):
        baseline, hit = (groups[mode]['elapsed_seconds']['median'] for mode in MODES)
        result['short_median_amortization_pure_build'] = amortization(build['build_seconds'], baseline, hit)
        result['short_median_amortization_eager_initialization'] = amortization(build['lookup_build_clone_seconds'], baseline, hit)
    result['amortization_notes'] = [
        'C + N*H versus N*B; minimum N for strict improvement = floor(C/(B-H))+1, only when B>H.',
        'Pure build excludes initial clone; eager initialization includes the measured lookup/build/clone then discarded clone.',
        'First-request totals and amortization are modeled from measured components; an integrated miss request was not timed.',
        'Assumes this prefix remains reusable for every request; does not estimate actual cache hit rate.',
    ]
    result['memory'] = {
        'logical_template_bytes': build['cache_bytes'],
        'logical_template_mib': build['cache_bytes'] / 1024**2,
        'logical_template_plus_request_clone_mib': 2 * build['cache_bytes'] / 1024**2,
        'run_rss_before_bytes': distribution(row['rss_before_bytes'] for row in runs),
        'run_rss_after_bytes': distribution(row['rss_after_bytes'] for row in runs),
        'process_cumulative_peak_rss_bytes': max((row['process_cumulative_peak_rss_bytes'] for row in runs), default=None),
        'rss_note': 'Both groups retain the base template. Hit RSS includes its still-live request clone; baseline internal generation cache may be released. These values are observations, not a net deployment-memory comparison. Peak is cumulative process peak.',
    }
    full_rows = [row for row in runs if row['full_completion']]
    has_checks = all(row.get('correctness') for row in full_rows) and len(full_rows) == 4
    completed = report.get('status') == 'completed' and not missing and not duplicate_keys and has_checks
    all_runtime = completed and all(correctness_summary(row)['expected_checks_passed'] for row in full_rows)
    all_semantic = completed and all(pair['semantic_match'] for pair in result['full_pairs'])
    result['validation'] = {
        'data_complete': completed,
        'all_pairs_exact_token_match': completed and all(pair['exact_token_match'] for pair in result['short_pairs'] + result['full_pairs']),
        'all_full_pairs_semantic_match': all_semantic,
        'all_expected_runtime_checks_passed': all_runtime,
        'full_completions_ended_with_eos': completed and all(row['ended_with_eos'] for row in full_rows),
    }
    if completed:
        result['status'] = 'passed' if all_runtime and all_semantic and result['validation']['full_completions_ended_with_eos'] else 'failed'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    target = args.output or args.results.with_name('summary.json')
    if args.results.resolve() == target.resolve():
        parser.error('output must differ from input results')
    raw = args.results.read_bytes()
    result = summarize(json.loads(raw))
    result['source_results_sha256'] = hashlib.sha256(raw).hexdigest()
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'status': result['status'], 'output': str(target), 'validation': result.get('validation')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
