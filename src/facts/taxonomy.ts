/**
 * facts/taxonomy.ts — the concept taxonomy: the ONLY place surface language
 * is gardened (docs/dict-vector.md). Generic activity/domain labels, each
 * carrying a curated GLOSS — the synonym/paraphrase surface for that concept.
 * Facts and rules reference labels; labels own their language. Atoms are
 * exact, primes are universal — the gloss is the synonymy bridge between
 * them.
 *
 * Discipline: labels are stable slugs (`ui.forms`); glosses are free text,
 * editable without breaking anything (vectors rebuild from them at
 * hydration). Extending the set is a rare, reviewable event — per-fact
 * surface forms are the exemplar creep this file exists to prevent.
 */

export interface TaxonomyLabel {
  label: string;
  gloss: string;
}

export const TAXONOMY: readonly TaxonomyLabel[] = [
  // ── building software ────────────────────────────────────────────────────
  { label: "ui.forms", gloss: "form forms signup sign-up login registration contact checkout address input field fields data entry validation submit checkbox dropdown select textarea survey questionnaire" },
  { label: "ui.layout", gloss: "layout page screen view component styling css design responsive grid flexbox theme dark mode navigation menu header footer modal dialog" },
  { label: "ui.web", gloss: "website web app frontend browser react vue html javascript typescript dom spa landing page" },
  { label: "code.review", gloss: "code review pull request pr diff approve merge feedback comments lgtm changeset" },
  { label: "code.testing", gloss: "test tests testing unit integration e2e coverage assertion mock fixture regression suite flaky ci checks" },
  { label: "code.style", gloss: "style convention naming lint linter formatting prettier eslint idiom pattern refactor clean code comments" },
  { label: "code.api", gloss: "api endpoint rest graphql route handler request response payload schema versioning contract webhook" },
  { label: "code.auth", gloss: "auth authentication authorization login token oauth session password credential permission role sso jwt" },
  { label: "data.database", gloss: "database db sql postgres schema table migration query index transaction orm sqlite backup" },
  { label: "data.analytics", gloss: "analytics metrics data analysis report dashboard chart graph visualization statistics trends kpi" },
  { label: "infra.deploy", gloss: "deploy deployment release ship rollout rollback production staging ci cd pipeline docker container kubernetes build artifact" },
  { label: "infra.ops", gloss: "monitoring alert incident outage oncall on-call logs observability uptime latency scaling server infrastructure runbook postmortem" },
  { label: "infra.security", gloss: "security vulnerability secret key rotation encryption audit compliance cve penetration firewall exposure leak" },
  // ── working together ─────────────────────────────────────────────────────
  { label: "comms.email", gloss: "email mail inbox reply thread cc bcc subject draft newsletter outreach" },
  { label: "comms.chat", gloss: "slack chat message dm channel ping thread mention discord teams" },
  { label: "comms.meetings", gloss: "meeting standup sync call agenda calendar invite schedule zoom recurring one-on-one retro retrospective" },
  { label: "comms.docs", gloss: "document doc documentation readme wiki notes spec proposal memo writeup guide manual onboarding" },
  { label: "work.planning", gloss: "plan planning roadmap milestone sprint backlog ticket task issue priority estimate deadline scope project management" },
  { label: "work.decisions", gloss: "decision adr tradeoff choice option evaluate compare pros cons rationale approve reject" },
  { label: "biz.customers", gloss: "customer client user account support ticket feedback complaint churn onboarding demo sales prospect" },
  { label: "biz.finance", gloss: "budget cost price billing invoice payment spend revenue subscription pricing quote expense" },
  { label: "biz.legal", gloss: "contract legal terms license agreement policy privacy gdpr compliance nda" },
  { label: "biz.people", gloss: "team hire hiring interview candidate role owner ownership responsibility org headcount manager" },
] as const;

export const TAXONOMY_LABELS: ReadonlySet<string> = new Set(TAXONOMY.map((t) => t.label));
