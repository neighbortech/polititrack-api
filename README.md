# PolitiTrack API — Vercel Deployment

## Deploy in 3 steps:

### 1. Push to GitHub
```bash
cd polititrack-api
git init
git add .
git commit -m "PolitiTrack API"
git remote add origin https://github.com/YOUR_USERNAME/polititrack-api.git
git push -u origin main
```

### 2. Deploy on Vercel
- Go to vercel.com → "Add New Project"
- Import your `polititrack-api` repo
- Add environment variable:
  - `FEC_API_KEY` = your FEC API key
- Click Deploy

### 3. Connect to your frontend
- Copy the URL Vercel gives you (e.g. `https://polititrack-api.vercel.app`)
- Go to your frontend project on Vercel (polititrack-web)
- Settings → Environment Variables
- Add: `VITE_API_URL` = `https://polititrack-api.vercel.app`
- Redeploy the frontend

## Test it
Visit `https://your-api-url.vercel.app/api/v1/people/search?name=elon+musk`

You should see real FEC donation data.
