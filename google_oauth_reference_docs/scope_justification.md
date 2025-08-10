# Google OAuth Scope Justification

**Requested Scope:** `https://www.googleapis.com/auth/gmail.readonly`

## Why We Request This Scope
We use read-only access to your Gmail messages solely to find the earliest "welcome" or "account creation" email from supported platforms (e.g., Gmail, Twitter, Reddit, Dropbox, Spotify, etc.).

## How We Use the Data
- Search for relevant account creation messages.
- Extract the date the message was received.
- Use this date to estimate your adoption percentile for that platform.

## Data Handling
- We do not store the content of any email.
- We store only the date and platform name for scoring purposes.
- No email content or metadata beyond the join date is retained.

## Benefit to the User
This scope allows us to verify your join date automatically, improving accuracy without requiring you to remember and enter dates manually.
