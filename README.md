# 6Frame Studio - Multi-Agent Social Marketing Hub

This is an in-house, standalone multi-agent orchestration tool and dashboard designed specifically for **6Frame Studio** to automate social media campaigns for visual assets. It allows uploading engineered video clips, extracting visual context via a multimodal Gemini 2.5 Pro agent, and generating optimized social copies (for LinkedIn, X/Twitter, and Instagram) with specialized Gemini 2.5 Flash agents.

## Features
- **Multimodal Video Analysis**: Ingests heavy MP4 files directly and uses Gemini 2.5 Pro to reason about frames, pacing, aesthetics, and themes.
- **Specialized Copywriting Agents**: Runs platform-specific prompts tailored to the B2B landscape of LinkedIn, viral hooks for X/Twitter, and aesthetic styles for Instagram.
- **Glassmorphic Local Dashboard**: Built with a sleek dark-themed, premium interface for dragging-and-dropping videos, watching live orchestration steps, reviewing staged content, and modifying copy before publishing.
- **Direct Platform API Integration**: Publishes staged posts to X (single tweets or threads) using `tweepy` and LinkedIn using `requests` REST API.
- **Mock Publish Sandbox Mode**: Allows sandbox execution and copy generation without needing live Twitter or LinkedIn API keys.

---

## Setup & Running Locally

Since the python dependencies have already been compiled and installed in your project's local virtual environment, you can run the server directly.

### 1. Set Workspace Folder
We recommend setting this directory as your active workspace folder in the Antigravity IDE:
`/Users/majid/.gemini/antigravity/scratch/marketing-automation`

### 2. Start the Server
Run the FastAPI development server from your terminal inside the project directory:
```bash
.venv/bin/python3.11 -m uvicorn main:app --reload --port 8000
```

### 3. Open the Dashboard
Navigate to your web browser:
[http://127.0.0.1:8000](http://127.0.0.1:8000)

---

## How to Use the Pipeline

1. **Configure API Keys**:
   - Click the **Settings** gear icon in the top right.
   - Enter your **Gemini API Key** (from Google AI Studio).
   - Enter your brand voice instructions (e.g. telling the agents to focus on Runway, Sora, Kling, or custom artistic style guides).
   - Enter Twitter and LinkedIn developer keys if you want to publish live. Leave **Enable Mock Publish Mode** checked to run in copy-paste mode without keys.
   - Click **Save Changes**.

2. **Run the Agents**:
   - Drag and drop your cinematic `.mp4` video into the drop zone.
   - Input your landing page or website URL (default: `https://6framestudio.com/`).
   - Click **Generate Social Campaigns**.
   - Watch the live orchestration status:
     - *Step 1*: Visual Asset uploading to the Gemini File API.
     - *Step 2*: Context Agent (Gemini 2.5 Pro) conducting deep multimodal analysis.
     - *Step 3*: Copywriting Agents (Gemini 2.5 Flash) writing copy in parallel.
     - *Step 4*: Social posts staged in the review dashboard.

3. **Review & Publish**:
   - Navigate through the LinkedIn, Twitter, and Instagram tabs to read the staged posts.
   - Edit the copy directly in the textboxes. The Twitter Thread view features active character counters for each tweet (highlighting red if you exceed X's 280-character limit).
   - Click **Publish to LinkedIn** or **Publish Thread to X** to execute the posting, or copy them to your clipboard.
