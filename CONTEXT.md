# Save Vibes MVP Product Requirements

## Product Summary

This product is a Django/Postgres web application for creating, generating, hosting, and governing AI-generated HTML reports.

The product lets semi-technical executives, sales leaders, finance leaders, and operators build or upload HTML/JavaScript reports, connect those reports to approved SQL data sources, cache the resulting data, and share the reports securely inside a company.

The core problem is unmanaged shadow reporting: HTML files, CSVs, SQL snippets, AI-generated dashboards, and one-off reports being passed around without hosting, permissioning, audit logs, cost controls, or security boundaries.

The product should provide the safe place where these reports live. It should let business users move quickly without forcing every reporting request through engineering, while giving IT, security, and engineering a governance layer.

The product name should be configurable in one place. Do not hard-code a brand name throughout templates, copy, models, or code paths.

## Target User Tension

The first target customer pattern has two sides:

Business users are semi-technical leaders who are dangerous enough to write SQL, use AI, and generate HTML reports, but who are not building production-grade internal tools. They are often trying to bypass a slow engineering or analytics backlog to answer an urgent business question.

IT, security, and engineering managers want to avoid shadow IT, uncontrolled credentials, expensive queries, unsafe data sharing, and ungoverned generated code, but they also do not want to block the business.

The product is not a traditional BI tool. It is a governed runtime for reports that business users are already going to create with AI.

## Core Technology Decisions

Use Django as the main application framework.

Use Postgres as the primary application database.

Store report HTML in Postgres for the MVP.

Store cached report data in Postgres for the MVP.

Compress cached report data using a Python-native compression library, preferably zstd.

Use SQLAlchemy for connecting to customer databases through SQLAlchemy-compatible connection strings.

Support common SQLAlchemy-compatible databases from the start where practical, especially Postgres, Snowflake, and BigQuery. The first implementation may exercise Postgres and SQLite more deeply, but the connection abstraction should not be limited to those.

Use a thin in-house AI provider abstraction for the MVP. Implement OpenAI first. Leave room to add Anthropic and Gemini later without binding the core app to a large orchestration framework.

Use Authlib or a similar low-level Python library for generic OAuth2/OIDC configuration. Okta-style enterprise SSO should be considered a key target, but the implementation should remain provider-generic.

Use Bootstrap, HTMX, server-rendered HTML, and small amounts of plain JavaScript for the product UI. The product application itself should not require a heavy frontend framework.

Deploy first on Heroku or another simple Django-compatible platform.

Keep the application architecture monolithic for the MVP.

The system should be compatible with async-friendly deployment where useful, especially for external API calls and database/cache operations. The exact WSGI/ASGI/Gunicorn/Uvicorn setup can be decided during implementation.

## Core User Types

The product should support three broad product roles.

Company admins manage authentication policy, users, database connections, AI provider keys, report permissions, query limits, and audit logs.

Creators generate, upload, edit, preview, and publish reports.

Viewers access reports that have been shared with them.

Django staff/superuser access is separate from company admin access. The Django admin should remain available for bootstrap and internal operations, but a company admin is not the same thing as a Django admin.

Every normal app user should belong to an organization/account. For the MVP, assume one primary organization per user unless implementation experience shows a need for multi-org switching.

## Authentication and Organization Access

The product should support company-level access control.

Users should log into the main app and only see reports, database connections, settings, and cached data they are authorized to access.

The app does not need tenant-specific URL paths for the MVP. Routes can be normal application routes such as:

```text
/builder/
/builder/new/
/builder/{id}/
/reports/
/reports/{id}/preview/
/settings/connections/
/settings/ai-providers/
/settings/audit/
```

Organization scope should be enforced through authentication, membership, and database permissions rather than URL structure.

The product should support generic OAuth2/OIDC configuration so customers can connect providers such as Okta, Google Workspace, Azure AD/Entra, Auth0, or similar identity providers.

Username/password login may exist for bootstrap, development, and accounts that allow it.

Organizations should have an `sso_required`-style policy flag. If SSO is required for an organization, ordinary username/password login should be blocked for users in that organization, while preserving necessary Django admin/bootstrap access.

## Database Connections

Company admins should be able to add database connections using SQLAlchemy-compatible connection strings.

The product should support arbitrary SQLAlchemy-compatible databases where the correct driver is installed.

The first-pass UX should make common providers easy, especially Postgres, Snowflake, and BigQuery, but the underlying design should not be limited to those providers.

Database credentials must be encrypted at rest before being stored in Postgres.

Database credentials must never be exposed to report HTML, browser JavaScript, logs, or viewers.

The product should strongly assume and recommend read-only database credentials.

Company admins should be able to test and disable database connections.

The MVP should include a seeded demo database option with fake SaaS sales data so users can try report generation before connecting their own database. Example entities include accounts, contacts, leads, opportunities, sales reps, pipeline stages, activities, bookings, and quota/target data.

## AI Provider Keys

Company admins should be able to add customer-owned OpenAI API keys for the MVP.

The code should use a thin provider abstraction so Anthropic, Gemini, or OpenAI-compatible endpoints can be added later.

Keys should be encrypted at rest before storage.

Keys should never be shown again after saving.

Creators should be able to choose an enabled provider/model when generating or revising a report. For the MVP this may be OpenAI-only.

The product should log AI generation activity, but never log raw API keys.

Avoid adding LangChain or LlamaIndex unless a concrete need emerges. The initial AI interface should be simple: generate a report, revise a report, and optionally review/rewrite SQL.

## Report Creation

Creators should be able to create reports in three ways:

Start from a prompt and generate the report with AI inside the product.

Paste or upload existing HTML and SQL, then use AI to adapt it to the product runtime.

Start from blank HTML.

AI generation is central to the MVP. The system should generate both the report HTML/JavaScript and the primary SQL dataset, then wire the HTML to the report runtime automatically.

AI generation should use the customer-configured OpenAI API key. The system should provide the model with instructions for how product reports work, including the data access SDK, security restrictions, cache behavior, and expectations around keeping datasets compact.

AI-generated reports should be saved as editable/versioned HTML.

A user should be able to revise an existing report with AI. Revisions should not destructively overwrite the previous version.

## Report Data Model

The default report pattern should be:

```text
HTML report + one primary SQL dataset
```

The primary SQL dataset is the main query that produces the report's data.

This is intentional. Most business users should not need to manage many component-level queries. A single report-level query is easier to understand, easier to cache, and easier for AI to preserve while iterating on the HTML.

The system may later support multiple named datasets or more advanced query patterns, but the MVP should optimize for one primary dataset per report.

The report JavaScript should access data through a product JavaScript SDK, conceptually like:

```javascript
const data = await sr.dataset("primary");
```

The exact SDK shape can be designed during implementation. The SDK name should also come from configurable product/runtime naming rather than a hard-coded brand.

## Query Execution

Reports should not connect directly to customer databases.

Report JavaScript should call product backend endpoints.

The backend should:

Authenticate the user.
Check report access.
Check database connection access.
Check cache.
If needed, execute the SQL through SQLAlchemy.
Apply query/result limits.
Store/cache the result.
Return the data to the browser.
Log the query execution.

The MVP can execute small/medium queries synchronously.

Long-running queries, scheduled refreshes, and durable background jobs can be added later if needed.

## Cached Data

For MVP, cached report data should be stored in Postgres.

Cached data should be serialized, compressed with zstd, and stored as binary data.

The system should track both raw size and compressed size.

Raw size matters because it approximates browser/network burden after decompression.

Compressed size matters because it affects Postgres storage.

The MVP should enforce conservative defaults so Postgres does not become an unbounded blob store and browsers are not asked to render unreasonable datasets.

Default limits should be defined as constants and copied into organization/account-level settings so company admins can tune them later.

Example policy concepts:

Query timeout, default around 120 seconds.
Maximum rows.
Maximum raw response size.
Maximum compressed cached size.
Cache TTL, default around 24 hours.
Maximum report cache footprint.

If a report needs to move hundreds of megabytes of raw data to the browser, the product should fail safely and explain that the report is outside the self-service governed-reporting scope and should involve engineering or analytics.

Expired cache cleanup can initially be handled simply, such as through a management command or scheduled job.

## Report Runtime and Security

Reports should run as untrusted HTML.

Reports should be rendered inside a sandboxed iframe or equivalent isolation boundary.

Reports should be served with a restrictive Content Security Policy.

Reports should be allowed to run inline JavaScript and render interactive HTML.

Reports should be allowed to call product-controlled report data endpoints needed for their own report data.

Reports should not be allowed to:

Call arbitrary external URLs.
Load arbitrary external scripts.
Submit external forms.
Access database credentials.
Access AI provider keys.
Access unauthorized app/session internals.

Reports should only access data through controlled product APIs.

Uploaded/generated HTML should be validated and warnings or blocks should be shown for obvious unsafe patterns, but CSP/runtime isolation should be the real enforcement layer.

## External Report Libraries

Reports often need visual polish. The product should support an allowlist for common external visualization and UI libraries rather than blocking all third-party assets forever.

The MVP should begin strict and controlled:

Allow inline report HTML, CSS, and JavaScript.
Allow calls to product-owned report data endpoints.
Block arbitrary external network calls.
Support a configurable allowlist for external script/style origins and exact asset URLs.

The allowlist should be administered by company admins or system admins, not by report viewers.

Common visualization libraries to consider for the default allowlist include libraries such as Chart.js, D3, Plotly, ECharts, Vega/Vega-Lite, Leaflet, Tabulator, DataTables, Grid.js, and similar widely used browser libraries. The implementation should prefer exact versioned CDN URLs or tightly scoped origins rather than broad wildcards.

AI-generated reports should know which libraries are allowed and should avoid using non-allowlisted assets.

## Data Governance and Limits

The system should assume that business users may write inefficient SQL or ask AI to create reports that try to move too much data.

The product should not try to perfectly optimize every report. It should prevent obvious disasters and provide useful remediation.

The system should support tunable limits at the organization/account or connection level, such as:

Query timeout.
Maximum rows.
Maximum raw result size.
Maximum compressed cache size.
Cache TTL.
Maximum report cache footprint.

If a report exceeds limits, the product should fail safely and explain the issue.

The product should suggest that the user aggregate more in SQL, select fewer columns, add filters, reduce row count, or ask AI to rewrite the report.

AI should bias toward pushing transformations, aggregation, filtering, and grouping into SQL rather than returning huge row-level datasets for browser-side processing.

AI can help review or rewrite SQL/HTML, but hard limits should enforce policy.

## Sharing

Sharing should feel closer to Google Docs than to a complex BI role model.

Reports should support:

Private drafts visible to the owner.
Explicit sharing with specific users.
Organization-wide publishing.

For the MVP, report roles are probably unnecessary. The owner can edit. Shared users and organization members can view according to the report's sharing state. Company admins can view, audit, disable, or block problematic reports.

## Audit and Monitoring

The product should log important events, including:

User login.
OAuth/OIDC login events.
Database connection created/tested/disabled.
AI provider key added/tested/disabled.
Report created/edited/generated/revised/published/viewed/blocked.
Dataset/query executed.
Cache hit/miss.
AI generation started/completed/failed.
Permission and sharing changes.
Limit failures and blocked unsafe runtime behavior.

Company admins should be able to view query logs and audit logs.

The audit trail is part of the product's core value proposition.

## MVP Success Criteria

The MVP is complete when a user can:

Log in, preferably through generic OAuth2/OIDC when configured.

Create or join an organization/account.

Configure whether SSO is required for the organization.

Add a SQLAlchemy-compatible database connection or use the seeded demo sales database.

Add an OpenAI API key.

Generate a report with AI from a prompt.

Have the generated report include HTML/JavaScript and a primary SQL dataset.

Preview the report in a sandboxed iframe.

Have the report load data through the product backend.

Have the backend run the SQL, compress and cache the result in Postgres, and return data.

Render the report in the browser.

Confirm a second page load uses the cached result.

Publish the report organization-wide or share it with specific users.

View the report as another authorized user.

Confirm arbitrary external network calls from the report are blocked.

Use allowlisted external visualization libraries where configured.

View query/audit logs as a company admin.

Block or disable a problematic report.

## First Build Milestone

The first working prototype should demonstrate the core loop:

System or company admin configures login and organization settings.

Company admin adds an OpenAI API key.

Company admin adds a database connection or enables the seeded demo sales database.

Creator asks AI to generate a simple SaaS sales-style report.

The generated report includes HTML/JS and a primary SQL dataset.

The report is saved in Postgres.

The report is previewed in a sandboxed iframe.

The report calls the product backend for data through the runtime SDK.

The backend runs the SQL, compresses and caches the result in Postgres, and returns data.

The browser renders the report.

A second page load uses the cached result.

The company admin can see the query log and report view log.

The runtime blocks non-allowlisted external network calls.

## Guiding Principle

Build the smallest useful version of a governed runtime for AI-generated HTML reports.

Favor simple, explicit Django/Postgres implementation over premature infrastructure.

Do not try to solve all future enterprise needs in the first build.

Focus on proving the core product loop: AI-generated HTML reports can be safely hosted, connected to approved data, cached, permissioned, shared, and audited.
