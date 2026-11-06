# Rule: Knowledge Base & RAG Context

Seed the PostgreSQL database on startup with at least 5 product articles for a fictional SaaS product ("CloudMetrics"):

1. Title: Exporting Reports to PDF/CSV
- URL: https://help.cloudmetrics.io/articles/export-pdf-csv
- Content: Instructions on navigating to Analytics > Reports, selecting date ranges, and clicking Export. Mentions 50,000 row limits.

2. Title: Two-Factor Authentication (2FA) Setup
- URL: https://help.cloudmetrics.io/articles/2fa-setup
- Content: Guide on enabling 2FA under Settings > Security using authenticator apps and emergency recovery codes.

3. Title: User Roles and Workspace Permissions
- URL: https://help.cloudmetrics.io/articles/roles-permissions
- Content: Details on Admin, Editor, and Viewer roles. Only workspace owners manage billing.

4. Title: Webhook Delivery Failures & Retries
- URL: https://help.cloudmetrics.io/articles/webhooks-retry
- Content: Explains the 2-second timeout, 3 automated retry attempts, and 15-minute pause after persistent 5xx.

5. Title: Subscription Billing & Invoices
- URL: https://help.cloudmetrics.io/articles/billing-invoices
- Content: Explains monthly renewal cycles, prorated upgrades, and invoice downloads.

Search Strategy:
- Use PostgreSQL full-text search or ILIKE keyword matching.
- Draft prompt must inject the matched article title and URL as citations.