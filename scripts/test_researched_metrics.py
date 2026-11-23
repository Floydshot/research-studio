"""Regression checks for issuer ROE, economic P/E N/A and daily freshness."""
from copy import deepcopy
from datetime import datetime, timezone
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_data as m

START = '2026-11-23T11:00:00+01:00'
FETCH = '2026-11-23T11:00:15+01:00'
END = '2026-11-23T11:01:00+01:00'
QUARTER = int(datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp())


def issuer(ticker):
    common = dict(validation_version=1, period_start='2026-01-01', period_end='2026-09-30',
                  as_of='2026-09-30', report_date='2026-11-05', retrieved_at=FETCH,
                  verified_at='2026-11-23', method='Reported issuer financial statements',
                  share_basis='ordinary shareholders')
    if ticker == 'NET':
        common.update(provider='Cloudflare IR / SEC', fact_type='gaap_ttm_earnings',
                      accounting_basis='US-GAAP', currency='USD',
                      period_start='2025-10-01', period='TTM bis 30.09.2026',
                      ttm_net_income_usd=-239050000,
                      source_url='https://www.sec.gov/Archives/edgar/data/1477333/000147733326000060/q326exhibit991.htm')
    else:
        common.update(provider='DBS Group Holdings', fact_type='issuer_reported_roe',
                      accounting_basis='IFRS', currency='SGD', value=17.0, annualized=True,
                      label='ROE · 9M 2026', period='9M 2026 · annualisiert',
                      source_url='https://www.dbs.com/iwov-resources/images/investors/quarterly-financials/2026/3Q26_trading_update.pdf')
    common['source_urls'] = [common['source_url']]
    return common


def stock(ticker):
    fields = dict(current_pe=30.0, forward_pe=20.0, price=100.0, sma200=95.0,
                  rsi14=50.0, currency='USD' if ticker == 'NET' else 'SGD',
                  ps_ratio=10.0, div_yield=0.0 if ticker == 'NET' else 4.0, roe=16.165)
    return dict(fields, ticker=ticker, name=ticker, hist_pe=40, source_status='live',
                as_of=FETCH, warnings=[], field_sources={field:'live' for field in fields},
                field_updated_at={field:FETCH for field in fields}, field_metadata={},
                returns=dict(source_status='live', retrieved_at=FETCH),
                dividend_growth=dict(source_status='live', retrieved_at=FETCH))


class ResearchMetricTests(unittest.TestCase):
    def apply(self, ticker, *, candidate=None, previous=None, now=END, info=None):
        current = stock(ticker)
        payload = dict(info=info or {'mostRecentQuarter':QUARTER})
        if candidate is not None:
            payload['net_earnings' if ticker == 'NET' else 'dbs_roe'] = candidate
        m.apply_researched_metrics(current, previous or {}, payload, now)
        return current

    def batch(self, current, group='tech'):
        payload = dict(portfolios=[dict(id=group, stocks=[current])])
        m.finalize_refresh_batch(payload, START, END)
        return current['refresh']

    def test_confirmed_loss_removes_positive_multiple_and_discount(self):
        current = self.apply('NET', candidate=issuer('NET'))
        self.assertIsNone(current['current_pe'])
        self.assertEqual(current['field_sources']['current_pe'], 'not_applicable')
        self.assertTrue(m.confirmed_nonpositive_pe(current, END))
        m.add_derived(current, 'tech')
        self.assertIsNone(current['chart_pe'])
        self.assertIsNone(current['spread_pe_pct'])

    def test_economic_na_is_complete_only_with_fresh_issuer_evidence(self):
        current = self.apply('NET', candidate=issuer('NET'))
        refresh = self.batch(current)
        self.assertEqual(refresh['status'], 'live')
        self.assertEqual(refresh['missing_fields'], [])
        self.assertEqual(refresh['not_applicable_fields'], ['current_pe'])
        self.assertIn('current_pe', refresh['required_fields'])

    def test_source_failure_keeps_old_date_and_remains_partial(self):
        old = issuer('NET')
        old.update(retrieved_at='2026-11-22T11:00:15+01:00')
        prior = self.apply('NET', candidate=old)
        current = self.apply('NET', previous=prior)
        self.assertEqual(current['field_updated_at']['current_pe'], old['retrieved_at'])
        self.assertEqual(self.batch(current)['missing_fields'], ['current_pe'])

    def test_unconfirmed_na_cannot_clear_a_missing_field(self):
        current = stock('NET')
        current.update(current_pe=None)
        current['field_sources']['current_pe'] = 'not_applicable'
        self.assertFalse(m.confirmed_nonpositive_pe(current, END))
        self.assertIn('current_pe', self.batch(current)['missing_fields'])

    def test_later_profitable_period_can_restore_numeric_pe(self):
        proof = issuer('NET')
        proof['ttm_net_income_usd'] = 10000000
        current = self.apply('NET', candidate=proof)
        self.assertEqual(current['current_pe'], 30)
        self.assertEqual(current['field_sources']['current_pe'], 'live')
        self.assertFalse(m.confirmed_nonpositive_pe(current, END))

    def test_newer_provider_quarter_rejects_old_loss(self):
        prior = self.apply('NET', candidate=issuer('NET'))
        q4 = int(datetime(2026, 12, 31, tzinfo=timezone.utc).timestamp())
        current = self.apply('NET', candidate=issuer('NET'), previous=prior,
                             now='2027-02-10T12:00:00+01:00', info={'mostRecentQuarter':q4})
        self.assertFalse(m.confirmed_nonpositive_pe(current, '2027-02-10T12:00:00+01:00'))

    def test_dbs_prefers_latest_reported_roe_with_explicit_period(self):
        current = self.apply('D05.SI', candidate=issuer('D05.SI'))
        self.assertEqual(current['roe'], 17.0)
        self.assertEqual(current['field_metadata']['roe']['label'], 'ROE · 9M 2026')
        self.assertEqual(current['field_updated_at']['roe'], FETCH)
        self.assertEqual(self.batch(current, 'value')['missing_fields'], [])

    def test_dbs_old_report_does_not_become_new_daily_evidence(self):
        prior = self.apply('D05.SI', candidate=issuer('D05.SI'))
        current = stock('D05.SI')
        current['roe'] = 17.0
        current['field_sources']['roe'] = 'previous'
        old = '2026-11-22T11:00:15+01:00'
        prior['field_metadata']['roe']['retrieved_at'] = old
        current['field_updated_at']['roe'] = old
        m.apply_researched_metrics(current, prior, {'info':{}}, END)
        self.assertEqual(current['field_metadata']['roe']['retrieved_at'], old)
        self.assertIn('roe', self.batch(current, 'value')['missing_fields'])

    def test_rejects_wrong_basis_currency_period_and_untrusted_sources(self):
        invalid = [dict(currency='USD'), dict(annualized=False), dict(value=True),
                   dict(period_end='2026-12-31'), dict(period_start='invalid'),
                   dict(retrieved_at='2026-11-24T12:00:00+01:00'),
                   dict(source_url='https://www.dbs.com.evil.example/report.pdf',
                        source_urls=['https://www.dbs.com.evil.example/report.pdf']),
                   dict(source_url='https://www.dbs.com:bad/report.pdf',
                        source_urls=['https://www.dbs.com:bad/report.pdf'])]
        for replacement in invalid:
            with self.subTest(replacement=replacement):
                candidate = {**issuer('D05.SI'), **replacement}
                self.assertIsNone(m.valid_researched_metric(candidate, 'D05.SI', 'roe', END))
        self.assertIsNone(m.valid_researched_metric(issuer('D05.SI'), 'D05.SI', 'roe', '2027-05-01T12:00:00+01:00'))

    def test_merge_cannot_resurrect_cloudflare_initial_260(self):
        seed = next(s for p in m.build_seed_data()['portfolios'] for s in p['stocks'] if s['ticker']=='NET')
        self.assertIsNone(seed['current_pe'])
        old = deepcopy(seed)
        old['current_pe'] = 260.0
        old['field_sources']['current_pe'] = 'initial'
        with patch.object(m, 'iso_now', return_value=END):
            merged = m.merge_stock(seed, old, {'warnings':[]}, 'tech', START)
        self.assertIsNone(merged['current_pe'])
        self.assertIsNone(merged['chart_pe'])

    def test_source_payload_is_not_mutated(self):
        candidate = issuer('NET')
        before = deepcopy(candidate)
        self.apply('NET', candidate=candidate)
        self.assertEqual(candidate, before)


if __name__ == '__main__':
    unittest.main()
