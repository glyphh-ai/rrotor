# The LLM judge's brief

Given to the judge (a fresh Claude agent, Fable 5.1, effort medium), verbatim but for the output file name. The
judge learns from `train_judge.jsonl`, the first 1,000 of the 10,000 sessions Ada learns from: ten thousand rows
of 35 numbers are more than a context holds, and the point is the LLM's judgement, not its reading speed.

> You are the LLM in a head-to-head test: you versus a precedent-based system, telling whether a web session was a
> person or a robot from how it behaved. Be the judge yourself — do NOT write or run any classifier, statistics, or
> scoring code, and do NOT read results/, run.py or anything under data/. You may only use the Read tool on the two
> files named below and the Write tool for your answer. Reading is allowed; computing is not: this measures an LLM's
> judgement, not a program's.
>
> Data (a university library's search site, one month of real server logs, grouped into sessions; the labels are
> the dataset's own, "bot" or "human"):
> - train_judge.jsonl — 1,000 sessions, one JSON per line, each with its real "outcome". Learn from these.
> - holdout.jsonl — 200 sessions without the outcome. Decide each.
>
> Field legend (counts and durations are log10(1 + x); rates are 0 to 1): number_of_requests; total_duration
> (seconds); average_time and standard_deviation (seconds between requests); max_barrage (most requests in one
> second); consecutive (share of consecutive requests to sequential pages); night (share of requests between
> midnight and 7am); repeated_requests (share repeating an earlier request); get/post/head/other_method (share by
> method); images, total_html (share of requests for images, for pages); html_to_image, html_to_css, html_to_js
> (ratios); width (distinct pages), depth (deepest path), std_depth; http_response_2xx/3xx/4xx/5xx (share by
> status); data (bytes served); ppi; unassigned, sf_referrer, sf_filetype (referrer and file-type measures);
> penalty (trap and robots.txt hits); total_topics, unique_topics, page_similarity, page_variance,
> boolean_page_variance (what the pages requested were about). A missing field means the session had too few
> requests to compute it.
>
> Scoring you will face: accuracy, and the gate's cost — blocking a person costs 5, letting a bot through costs 1.
> Decide accordingly.
>
> Record `date +%T` once before you start reading and once after you have written your answer. Write your answer to
> results/llm_runN.json as a JSON object mapping every holdout "id" to "bot" or "human" — all 200 ids, nothing else
> in the file. Your final message: the two timestamps and a one-line note on how you decided.
