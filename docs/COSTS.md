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
| Google search + Claude reading the page | ~$0.0007 (Claude, per page read) + ~$0.015 per search / ~3-5 new jobs per search = ~$0.004-$0.005 | ~10 results per search, only some are new real jobs |

"5,000 jobs a day" example:

| Mix | Searches/day | Search $/mo | Claude $/mo | Total $/mo |
|---|---|---|---|---|
| All 5,000 from Google | ~1,250 (37,500/mo) | ~$560 | ~$105 (one page read per job, 2,600 in / 900 out at $0.10 / $0.50) | **~$665** |
| 3,500 from free feeds, 1,500 from Google | ~375 (11,000/mo) | ~$170 | ~$32 | **~$200** |
| Today's plan (166 queries, twice a day), only if the schedule is turned on | ~332 (10,000/mo) | ~$149 | depends on new pages | **search ~$149, plus Claude** |

Reality check: the free feeds produced about 95 jobs in total. The number of genuinely new copywriter / email / funnel hiring posts per day
across the whole internet is probably in the low hundreds, so spend is capped by supply of jobs, not by budget. Start small and use the
cost report to see cost per new job before scaling queries.

## Customer-run searches
A live search runs the customer's words on the open web plus LinkedIn posts, LinkedIn jobs, X, Facebook, Reddit hiring threads (r/forhire and r/hiring), and 15 remote job boards (22 Google searches). Search cost to us is about $0.33, plus extraction. The customer pays 1.5x, about $0.73, shown as the estimate before they run. A search aimed at one site is a single Google search.
Repeating a search within 6 hours is served from the database and costs nobody anything.

## Levers, in order of effect
1. More free feeds (cost $0).
2. Back-off of dead queries (built): spend follows yield.
3. Cache customer searches (built): popular searches get cheaper.
4. Snippet-only extraction for social posts (built for X/LinkedIn) and Anthropic batch pricing for scheduled extraction (not built).
5. Move to a bigger SerpAPI plan or a cheaper provider once volume justifies it (Serper is already supported in the code).


## Customer-paid searches (live search)
Customers pay for their own searches from prepaid credits. Charge = max(fee + extraction, 1.5 × our real cost), once per Google search that actually ran. A default search is 22 Google searches (the open web, LinkedIn posts, LinkedIn jobs, X, Facebook, r/forhire, r/hiring, and 15 remote boards: Dynamite Jobs, Himalayas, NoDesk, Working Nomads, We Work Remotely, 4 Day Week, Workable, Content Jobs, Jobspresso, EU Remote Jobs, Remote.co, Remote Rocketship, Jobicy, Remote OK, and Remotive). The estimate is about $0.73. One chosen site is still one Google search. Remotely and Creator Job Board are not included: a `hiring copywriter` search on those sites returned no individual jobs. The scheduled Google plan
stays OFF (`GOOGLE_SCHEDULE_ENABLED=0`) so API keys alone never spend your money on a timer. Turning it on would run 166 queries twice a day (about $149/month in searches before Claude). The real margin is shown on the admin
dashboard and in `scripts/cost_report.py`; it must read 1.50×.
