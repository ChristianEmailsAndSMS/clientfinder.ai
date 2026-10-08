# What it costs to run (estimates, not measurements)

**Unverified inputs.** SerpAPI price (~$75 for 5,000 searches = $0.015/search; bigger plans cost less per search, price unknown),
Claude Haiku 5.5 price ($0.10 / $0.50 per million tokens in/out, as configured in `app/pricing.py`; extraction is ~10× cheaper than with Haiku 4.5, ≈ $0.0005 per page) and tokens per page (~2,600 in, ~900 out incl. thinking). Check all three, then
re-run `backend/scripts/cost_report.py` after a week of real use.

## Cost does not grow with customers
Everything customers do inside the database (search, filter, sort, open posts) is a database read: effectively free. Money is only spent
on **collecting** jobs. So 10 customers or 10,000 cost the same to serve; collection cost is fixed by how much you collect.

## Collecting N jobs per day
| Source | Cost per job | Notes |
|---|---|---|
| Free feeds (RemoteOK, WWR, Remotive, Greenhouse, Lever, Ashby, ProBlogger) | $0 | Structured data, no Claude call |
| Google search + Claude reading the page | ~$0.004 (Claude) + ~$0.015 per search / ~3-5 new jobs per search = ~$0.004-$0.005 | ~10 results per search, only some are new real jobs |

"5,000 jobs a day" example:

| Mix | Searches/day | Search $/mo | Claude $/mo | Total $/mo |
|---|---|---|---|---|
| All 5,000 from Google | ~1,250 (37,500/mo) | ~$560 | ~$600 (about half with snippet-only or batch extraction) | **~$1,150** |
| 3,500 from free feeds, 1,500 from Google | ~375 (11,000/mo) | ~$170 | ~$180 | **~$350** |
| Today's plan (34 queries, twice a day) | ~68 (2,040/mo) | ~$31 | small | **~$35-45** |

Reality check: the free feeds produced about 95 jobs in total. The number of genuinely new copywriter / email / funnel hiring posts per day
across the whole internet is probably in the low hundreds, so spend is capped by supply of jobs, not by budget. Start small and use the
cost report to see cost per new job before scaling queries.

## Customer-run searches
A live search costs us ~$0.015 (search) + ~$0.004 per page read (about 10) = about $0.055; customers are charged 1.5x = about $0.08.
Repeating a search within 6 hours is served from the database and costs nobody anything.

## Levers, in order of effect
1. More free feeds (cost $0).
2. Back-off of dead queries (built): spend follows yield.
3. Cache customer searches (built): popular searches get cheaper.
4. Snippet-only extraction for social posts (built for X/LinkedIn) and Anthropic batch pricing for scheduled extraction (not built).
5. Move to a bigger SerpAPI plan or a cheaper provider once volume justifies it (Serper is already supported in the code).


## Customer-paid searches (live search)
Customers pay for their own searches from prepaid credits. Charge = max(fee + extraction, 1.5 × our real cost). With Haiku 5.5 the SerpAPI
search ($0.015) dominates, so a typical 10-result search costs us about $0.02 and the customer about $0.033. The scheduled Google plan
stays OFF (`GOOGLE_SCHEDULE_ENABLED=0`) so API keys alone never spend your money on a timer. The real margin is shown on the admin
dashboard and in `scripts/cost_report.py`; it must read 1.50×.
