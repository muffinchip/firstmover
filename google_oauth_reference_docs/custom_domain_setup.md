# Setting Up a Custom Domain on Render and Verifying in Google Search Console

## Step 1: Add a Custom Domain in Render
1. Go to your service in Render.
2. Click **Settings**.
3. Under **Custom Domains**, click **Add Custom Domain**.
4. Enter your domain name (e.g., `firstmover.yourdomain.com`).
5. Render will show the DNS records to add.

## Step 2: Update DNS Records
1. Log into your domain registrar (GoDaddy, Namecheap, etc.).
2. Add the CNAME or A records provided by Render.
3. Wait for DNS propagation (may take up to 24 hours).

## Step 3: Verify Your Domain in Google Search Console
1. Go to [Google Search Console](https://search.google.com/search-console/welcome).
2. Click **Add property**.
3. Choose **Domain** or **URL prefix** (use URL prefix if verifying only the app subdomain).
4. If using URL prefix, enter your app's custom domain (e.g., `https://firstmover.yourdomain.com`).
5. Select **HTML tag** or **DNS record** method for verification.
6. Follow the instructions—if using DNS, you can reuse the TXT record you add here for Render.

## Step 4: Add the Domain to Your OAuth Consent Screen
1. In [Google Cloud Console](https://console.cloud.google.com/), go to **APIs & Services → OAuth consent screen**.
2. Add your verified domain under **Authorized domains**.
3. Update your **App homepage**, **Privacy policy**, and **Terms of Service** URLs to point to pages hosted on your verified domain.

## Step 5: Publish the App
- Once your domain is verified and your consent screen is complete, submit for verification to make the app available to the public.
