#!/usr/bin/env python3
"""
Storylane Demo Classifier
=========================
Scrapes the Storylane customer showcase, walks through each demo,
captures screenshots of every step, and classifies them using Claude.

Usage:
    python3 run.py                  # Run full pipeline
    python3 run.py --scrape-only    # Only scrape demo URLs (no walking/classifying)
    python3 run.py --limit 5        # Only process first 5 demos
    python3 run.py --demo-url URL   # Process a single demo URL directly
"""

import argparse
import asyncio
import base64
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SHOWCASE_URL = "https://www.storylane.io/customer-showcase"
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
OUTPUT_DIR = Path(__file__).parent / "output"
CLASSIFICATION_CRITERIA_FILE = Path(__file__).parent / "classification_criteria.txt"
CUSTOM_RUBRICS_DIR = Path(__file__).parent / "rubrics"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ---------------------------------------------------------------------------
# Model future-proofing: known models, runtime detection, fallback chain
# ---------------------------------------------------------------------------

# Known models (newest first) — update this list when new models are released
KNOWN_MODELS = {
    "haiku": [
        {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5"},
    ],
    "sonnet": [
        {"id": "claude-sonnet-4-6-20250627", "label": "Claude Sonnet 4.6"},
        {"id": "claude-sonnet-4-20250514", "label": "Claude Sonnet 4"},
    ],
}

# Fallback chains: if a model is deprecated, try these alternatives in order
MODEL_FALLBACKS = {
    "claude-haiku-4-5-20251001": [],
    "claude-sonnet-4-6-20250627": ["claude-sonnet-4-20250514"],
    "claude-sonnet-4-20250514": ["claude-sonnet-4-6-20250627"],
}

# Cache for detected models (populated at runtime)
_detected_models = {"haiku": None, "sonnet": None}


def detect_available_models(api_key: str) -> dict:
    """Query the Anthropic API to discover available models. Returns dict of tier -> model_id."""
    import anthropic
    detected = {}
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.models.list(limit=100)
        available_ids = {m.id for m in response.data}

        for tier, models in KNOWN_MODELS.items():
            for model in models:
                if model["id"] in available_ids:
                    detected[tier] = model["id"]
                    break
    except Exception as e:
        print(f"   ⚠️  Could not detect models via API: {e}")

    return detected


def get_model(tier: str, api_key: str) -> str:
    """Get the best available model for a tier (haiku/sonnet), with fallback."""
    global _detected_models

    # Try cached detection first
    if _detected_models.get(tier):
        return _detected_models[tier]

    # Try runtime detection
    if api_key:
        detected = detect_available_models(api_key)
        _detected_models.update(detected)
        if tier in detected:
            return detected[tier]

    # Fall back to first known model for the tier
    return KNOWN_MODELS[tier][0]["id"]


def call_with_fallback(client, model_id: str, **kwargs):
    """Call client.messages.create with automatic fallback on model deprecation errors."""
    try:
        return client.messages.create(model=model_id, **kwargs)
    except Exception as e:
        error_str = str(e).lower()
        if "not found" in error_str or "deprecated" in error_str or "not available" in error_str:
            fallbacks = MODEL_FALLBACKS.get(model_id, [])
            for fb in fallbacks:
                print(f"   ⚠️  Model {model_id} unavailable, trying {fb}...")
                try:
                    return client.messages.create(model=fb, **kwargs)
                except Exception:
                    continue
        raise


# Playwright settings
HEADLESS = True
VIEWPORT = {"width": 1440, "height": 900}
STEP_TIMEOUT_MS = 8000      # max wait for next button to appear
PAGE_LOAD_TIMEOUT_MS = 30000
STEP_TRANSITION_WAIT_MS = 1500  # wait after clicking next for animation

# Max steps to walk per demo (safety limit)
MAX_STEPS_PER_DEMO = 40

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DemoInfo:
    """Info about a demo from the showcase page."""
    name: str
    showcase_url: str
    demo_iframe_url: str = ""
    demo_domain: str = ""
    live_preview_url: str = ""
    category: str = ""
    is_accessible: bool = True
    is_gated: bool = False
    error: str = ""

@dataclass
class DemoStep:
    """A single step in a demo."""
    step_number: int
    total_steps: int
    tooltip_text: str = ""
    screenshot_path: str = ""
    has_hotspot: bool = False
    has_next_button: bool = False

@dataclass
class DemoResult:
    """Full result for a demo after walking through it."""
    info: DemoInfo
    steps: list = field(default_factory=list)
    total_steps_found: int = 0
    steps_captured: int = 0
    classification: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# STEP 1: Scrape showcase page for all demo URLs
# ---------------------------------------------------------------------------

async def scrape_showcase(page) -> list[DemoInfo]:
    """Scrape the customer showcase page and extract all demo card links."""
    print("\n📋 STEP 1: Scraping showcase page...")
    print(f"   Navigating to {SHOWCASE_URL}")

    await page.goto(SHOWCASE_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
    await page.wait_for_timeout(2000)

    # Extract all demo links from the Interactive Demos tab
    demos = await page.evaluate("""() => {
        const links = document.querySelectorAll('a[href*="/customer-showcase/"]');
        const seen = new Set();
        const results = [];
        links.forEach(a => {
            const href = a.href;
            if (seen.has(href) || href === window.location.href) return;
            seen.add(href);

            // Try to get the company name from the card
            const card = a.closest('[class*="card"], [class*="Card"], div') || a;
            const nameEl = card.querySelector('h2, h3, h4, [class*="name"], [class*="Name"]');
            const name = nameEl ? nameEl.textContent.trim() : href.split('/').pop();

            // Try to get category
            const catEl = card.querySelector('[class*="category"], [class*="tag"], [class*="industry"]');
            const category = catEl ? catEl.textContent.trim() : '';

            results.push({
                name: name,
                showcase_url: href,
                category: category
            });
        });
        return results;
    }""")

    demo_list = []
    for d in demos:
        demo_list.append(DemoInfo(
            name=d["name"],
            showcase_url=d["showcase_url"],
            category=d.get("category", "")
        ))

    print(f"   ✅ Found {len(demo_list)} demos on showcase page")
    for i, d in enumerate(demo_list):
        print(f"      {i+1}. {d.name}")

    return demo_list


# ---------------------------------------------------------------------------
# STEP 2: Visit each showcase page and extract the demo iframe URL
# ---------------------------------------------------------------------------

async def extract_demo_url(page, demo: DemoInfo) -> DemoInfo:
    """Visit a showcase page and find the Storylane demo iframe URL."""
    try:
        await page.goto(demo.showcase_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        await page.wait_for_timeout(2000)

        result = await page.evaluate("""() => {
            const iframes = document.querySelectorAll('iframe');
            let demoUrl = '';
            let demoDomain = '';

            for (const iframe of iframes) {
                try {
                    const url = new URL(iframe.src);
                    // Look for demo iframes (not hub iframes)
                    if (url.pathname.includes('/demo/')) {
                        demoUrl = iframe.src;
                        demoDomain = url.hostname;
                        break;
                    }
                } catch(e) {}
            }

            // Get the "View live preview" link
            let livePreviewUrl = '';
            const links = document.querySelectorAll('a');
            for (const a of links) {
                if (a.textContent.includes('View live')) {
                    livePreviewUrl = a.href;
                    break;
                }
            }

            // Check for gating (forms/lead capture)
            const forms = document.querySelectorAll('form, [class*="gate"], [class*="Gate"], [class*="leadCapture"]');
            const isGated = forms.length > 0;

            return { demoUrl, demoDomain, livePreviewUrl, isGated };
        }""")

        demo.demo_iframe_url = result["demoUrl"]
        demo.demo_domain = result["demoDomain"]
        demo.live_preview_url = result["livePreviewUrl"]
        demo.is_gated = result["isGated"]

        if not demo.demo_iframe_url:
            demo.is_accessible = False
            demo.error = "No demo iframe found on showcase page"

    except Exception as e:
        demo.is_accessible = False
        demo.error = f"Failed to load showcase page: {str(e)[:100]}"

    return demo


# ---------------------------------------------------------------------------
# STEP 3: Walk through a demo — click Next, capture screenshots
# ---------------------------------------------------------------------------

async def walk_demo(page, demo: DemoInfo, demo_index: int) -> DemoResult:
    """Navigate a Storylane demo step by step, capturing screenshots."""
    result = DemoResult(info=demo)

    if not demo.demo_iframe_url:
        print(f"   ⏭️  Skipping {demo.name} — no demo URL found")
        return result

    demo_dir = SCREENSHOTS_DIR / _safe_filename(demo.name)
    demo_dir.mkdir(parents=True, exist_ok=True)

    try:
        print(f"   🔗 Loading demo: {demo.demo_iframe_url[:80]}...")
        await page.goto(demo.demo_iframe_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        await page.wait_for_timeout(3000)

        # Check if demo loaded as an interactive Storylane demo (not just an image/GIF)
        demo_check = await page.evaluate("""() => {
            const hasPlayer = document.querySelector('[data-testid="demoplayer-image"], [class*="DemoPlayer"], [class*="Widget"]') !== null;
            const hasNextBtn = document.querySelector('[data-testid="widget-cta"]') !== null;
            const hasHotspot = document.querySelector('[class*="Hotspot"], [class*="hotspot"]') !== null;
            const hasTooltip = document.querySelector('[class*="Tooltip"], [class*="tooltip"]') !== null;

            // Check if it's just a static image/video/GIF with no interactive elements
            const isInteractive = hasNextBtn || hasHotspot || hasTooltip;

            return { hasPlayer, isInteractive };
        }""")

        if not demo_check["hasPlayer"] or not demo_check["isInteractive"]:
            # Check if it's a gated/form page
            has_form = await page.evaluate("""() => {
                return document.querySelector('form, input[type="email"], [class*="leadCapture"], [class*="gate"]') !== null;
            }""")
            if has_form:
                demo.is_gated = True
                demo.error = "Demo is gated (requires form submission)"
                print(f"   🔒 Demo is gated — skipping")
                return result
            elif demo_check["hasPlayer"] and not demo_check["isInteractive"]:
                demo.is_accessible = False
                demo.error = "Not an interactive demo (static image/GIF/video only)"
                print(f"   🖼️  Static content (not interactive) — skipping")
                return result
            else:
                demo.is_accessible = False
                demo.error = "Demo did not load — no player elements found"
                print(f"   ❌ Demo did not load")
                return result

        # Walk through steps
        step_num = 0
        while step_num < MAX_STEPS_PER_DEMO:
            step_num += 1

            # Extract current step info
            step_info = await page.evaluate("""() => {
                // Get tooltip text
                const tooltip = document.querySelector(
                    '[class*="TooltipPositionManager"], [class*="WidgetManager"], [class*="ModalWidget"]'
                );
                const tooltipText = tooltip ? tooltip.textContent.trim() : '';

                // Parse step counter (e.g. "2/13")
                const pageMatch = tooltipText.match(/(\\d+)\\/(\\d+)/);
                const currentStep = pageMatch ? parseInt(pageMatch[1]) : 0;
                const totalSteps = pageMatch ? parseInt(pageMatch[2]) : 0;

                // Check for Next button
                const nextBtn = document.querySelector('[data-testid="widget-cta"]');
                const hasNext = nextBtn !== null && nextBtn.offsetParent !== null;
                const nextBtnText = nextBtn ? nextBtn.textContent.trim() : '';

                // Check for hotspot
                const hotspot = document.querySelector(
                    '[class*="HotspotLegacy_beaconClickableArea"], [class*="WidgetHotspotBeacon"]'
                );
                const hasHotspot = hotspot !== null;

                // Check for gating form
                const hasForm = document.querySelector(
                    'form, input[type="email"], [class*="leadCapture"]'
                ) !== null;

                return {
                    tooltipText: tooltipText.substring(0, 500),
                    currentStep, totalSteps,
                    hasNext, nextBtnText,
                    hasHotspot,
                    hasForm
                };
            }""")

            # If we hit a form/gate mid-demo, stop
            if step_info["hasForm"]:
                print(f"   🔒 Hit a gated form at step {step_num} — stopping")
                demo.is_gated = True
                break

            total = step_info["totalSteps"] or step_num
            current = step_info["currentStep"] or step_num

            # Capture screenshot
            screenshot_path = demo_dir / f"step_{step_num:03d}.png"
            await page.screenshot(path=str(screenshot_path), full_page=False)

            step = DemoStep(
                step_number=current,
                total_steps=total,
                tooltip_text=step_info["tooltipText"],
                screenshot_path=str(screenshot_path),
                has_hotspot=step_info["hasHotspot"],
                has_next_button=step_info["hasNext"],
            )
            result.steps.append(step)
            result.total_steps_found = total
            result.steps_captured = len(result.steps)

            print(f"      📸 Step {current}/{total}: {step_info['tooltipText'][:60]}...")

            # Decide how to advance to next step
            if step_info["hasNext"]:
                # Click the Next/CTA button
                try:
                    btn = page.locator('[data-testid="widget-cta"]')
                    await btn.click(timeout=STEP_TIMEOUT_MS)
                    await page.wait_for_timeout(STEP_TRANSITION_WAIT_MS)
                except Exception:
                    # Try clicking hotspot as fallback
                    try:
                        hotspot = page.locator('[class*="HotspotLegacy_beaconClickableArea"]').first
                        await hotspot.click(timeout=3000)
                        await page.wait_for_timeout(STEP_TRANSITION_WAIT_MS)
                    except Exception:
                        print(f"      ⚠️  Could not advance past step {step_num}")
                        break

            elif step_info["hasHotspot"]:
                # Click the hotspot beacon
                try:
                    hotspot = page.locator('[class*="HotspotLegacy_beaconClickableArea"]').first
                    await hotspot.click(timeout=STEP_TIMEOUT_MS)
                    await page.wait_for_timeout(STEP_TRANSITION_WAIT_MS)
                except Exception:
                    print(f"      ⚠️  Could not click hotspot at step {step_num}")
                    break
            else:
                # No next button and no hotspot — we've reached the end
                print(f"      ✅ Reached end of demo at step {step_num}")
                break

            # Safety: check if step counter hasn't changed (stuck)
            if step_info["totalSteps"] > 0 and current >= step_info["totalSteps"]:
                print(f"      ✅ Completed all {total} steps")
                break

    except Exception as e:
        demo.error = f"Error walking demo: {str(e)[:200]}"
        print(f"   ❌ Error: {demo.error}")

    return result


# ---------------------------------------------------------------------------
# STEP 4: Classify demos using Claude API
# ---------------------------------------------------------------------------

def generate_rubric_from_doc(doc_text: str, output_path: Path = None, api_key: str = None) -> str:
    """
    Use Sonnet to convert a raw framework document into a structured classification rubric.
    Returns the generated rubric text and saves it to output_path if provided.
    """
    import anthropic

    effective_key = api_key or ANTHROPIC_API_KEY
    if not effective_key:
        raise ValueError("No API key provided — cannot generate rubric")

    print("   🧠 Generating classification rubric from uploaded document (using Sonnet)...")

    prompt = f"""You are an expert at creating evaluation rubrics for interactive product demos.

The user has provided a framework document that describes how they want demos to be evaluated. Your job is to convert this into a clean, structured classification rubric that an AI can use to consistently evaluate product demos.

## Your output MUST include:

1. **A brief intro** (1-2 sentences) explaining what is being evaluated and the core framework.

2. **Classification Buckets** — Extract the key categories/types from the document. Each bucket should have:
   - A clear name
   - A description of what makes a demo fall into this bucket
   - Specific signals/indicators to look for (bullet points)
   - Aim for 4-7 buckets total. Always include a "Gated / Inaccessible" bucket at the end.

3. **Evaluation Dimensions** — Extract the scoring dimensions. Each should be:
   - A snake_case name (e.g., logic_score, emotion_score)
   - A clear description of what 1 vs 10 means
   - Aim for 4-8 dimensions.

4. **Output Format** — Always end with this exact JSON schema:
```
## Output Format

Respond in JSON:
{{
  "type": "one of the classification buckets above",
  "overall_score": 1-10,
  [one key per evaluation dimension, e.g. "logic_score": 1-10],
  "summary": "2-3 sentence summary of what the demo shows and how it tells its story",
  "strengths": ["list of specific things done well"],
  "weaknesses": ["list of specific areas for improvement"],
  "recommendation": "One specific actionable suggestion to improve this demo"
}}
```

## Rules:
- Be faithful to the user's framework — don't invent categories they didn't describe
- If the document is vague in some areas, make reasonable inferences but stay true to the intent
- Use clear, concrete language. Avoid jargon unless the doc uses it.
- The rubric should be self-contained — someone reading it should fully understand how to classify a demo without seeing the original document.

## The user's framework document:

{doc_text}
"""

    effective_key = api_key or ANTHROPIC_API_KEY
    client = anthropic.Anthropic(api_key=effective_key)
    model_id = get_model("sonnet", effective_key)
    print(f"   Using model: {model_id}")
    response = call_with_fallback(
        client, model_id,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )

    rubric = response.content[0].text.strip()

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rubric)
        print(f"   ✅ Rubric saved to {output_path}")

    return rubric


def load_classification_criteria(custom_rubric_path: str = None) -> str:
    """Load classification criteria from a custom rubric file, the default file, or built-in fallback."""
    # 1. Custom rubric file (from --criteria-file or web upload)
    if custom_rubric_path:
        p = Path(custom_rubric_path)
        if p.exists():
            criteria = p.read_text().strip()
            if criteria:
                print(f"   📄 Loaded custom rubric from {p.name}")
                return criteria

    # 2. Default classification_criteria.txt
    if CLASSIFICATION_CRITERIA_FILE.exists():
        criteria = CLASSIFICATION_CRITERIA_FILE.read_text().strip()
        if criteria:
            print(f"   📄 Loaded classification criteria from {CLASSIFICATION_CRITERIA_FILE.name}")
            return criteria

    # Default classification criteria
    return """
Classify this interactive product demo into one of the following categories based on the screenshots and tooltip text from each step:

## Demo Types
1. **Storytelling Demo (Good)**: Has a clear narrative arc. Starts with context/problem, walks through the solution step-by-step, and ends with value/outcome. Tooltip text guides the viewer with explanatory copy. Steps flow logically.
2. **Storytelling Demo (Needs Improvement)**: Attempts storytelling but has gaps — missing context, abrupt transitions, unclear copy, too many steps without purpose, or tooltips that just describe UI elements instead of telling a story.
3. **Feature Walkthrough**: A straightforward tour of product features. Shows what the product does without a narrative arc. Functional but not engaging as a story.
4. **Click-Through Demo**: Minimal guidance. Just hotspots to click with little to no tooltip text. Feels like a slideshow rather than a guided experience.
5. **Gated/Inaccessible**: Demo requires form submission or is otherwise not fully accessible.

## Evaluation Criteria
- **Narrative flow**: Does the demo tell a story from problem to solution?
- **Copy quality**: Are tooltips informative, concise, and guiding? Or generic/empty?
- **Step progression**: Do steps build on each other logically?
- **Visual quality**: Are screenshots clean, focused, and well-composed?
- **Length**: Is the demo an appropriate length (not too short, not tediously long)?
- **Call to action**: Does it end with a clear next step?
"""


async def classify_demo(demo_result: DemoResult, mode: str = "fast", criteria_file: str = None, api_key: str = None) -> dict:
    """
    Classify a demo using Claude.

    Modes:
      - "fast":  Text-only with Haiku (~$0.002 per demo)
      - "full":  Screenshots + text with Sonnet (~$0.15 per demo)
      - "smart": Text-only with Haiku first; caller handles Sonnet follow-up for top demos
    """
    import anthropic

    effective_key = api_key or ANTHROPIC_API_KEY
    if not effective_key:
        return {"error": "No API key configured", "type": "unclassified"}

    if not demo_result.steps:
        return {"type": "inaccessible", "reason": demo_result.info.error or "No steps captured"}

    criteria = load_classification_criteria(criteria_file)

    use_screenshots = (mode == "full")
    tier = "haiku" if mode in ("fast", "smart") else "sonnet"
    model = get_model(tier, effective_key)
    model_label = "Haiku" if "haiku" in model else "Sonnet"

    # Build the message content
    content = []

    if use_screenshots:
        intro = f"""You are analyzing an interactive product demo from "{demo_result.info.name}".

{criteria}

Below are screenshots and tooltip text from each step of the demo ({len(demo_result.steps)} steps total). Analyze them and classify this demo.
"""
    else:
        intro = f"""You are analyzing an interactive product demo from "{demo_result.info.name}".

{criteria}

Below is the tooltip/guide text from each step of the demo ({len(demo_result.steps)} steps total). You don't have screenshots, but the tooltip text reveals the narrative structure, copy quality, persona targeting, and proof elements. Classify this demo based on the text content.
"""

    content.append({"type": "text", "text": intro})

    # Add steps
    steps_to_send = demo_result.steps
    if use_screenshots and len(steps_to_send) > 15:
        # Sample evenly for screenshot mode
        indices = [0]
        step_size = (len(steps_to_send) - 1) / 14
        for i in range(1, 14):
            indices.append(round(i * step_size))
        indices.append(len(steps_to_send) - 1)
        indices = sorted(set(indices))
        steps_to_send = [demo_result.steps[i] for i in indices]

    step_texts = []
    for step in steps_to_send:
        step_text = f"--- Step {step.step_number}/{step.total_steps} ---\nTooltip: {step.tooltip_text}"
        step_texts.append(step_text)

        if use_screenshots:
            content.append({"type": "text", "text": f"\n{step_text}\n"})
            screenshot_path = Path(step.screenshot_path)
            if screenshot_path.exists():
                img_data = base64.standard_b64encode(screenshot_path.read_bytes()).decode("utf-8")
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": img_data,
                    }
                })

    if not use_screenshots:
        # Text-only: send all step texts as one block (much cheaper)
        all_steps_text = "\n\n".join(step_texts)
        content.append({"type": "text", "text": all_steps_text})

    content.append({
        "type": "text",
        "text": "\nNow classify this demo. Respond ONLY in JSON format matching the output schema in the criteria above."
    })

    try:
        client = anthropic.Anthropic(api_key=effective_key)
        response = call_with_fallback(
            client, model,
            max_tokens=1500,
            messages=[{"role": "user", "content": content}],
        )

        response_text = response.content[0].text.strip()
        # Try to parse JSON from response
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()

        classification = json.loads(response_text)
        classification["_model"] = model_label
        classification["_mode"] = mode
        return classification

    except json.JSONDecodeError:
        return {"type": "unclassified", "raw_response": response_text[:500], "_model": model_label}
    except Exception as e:
        return {"error": str(e)[:200], "type": "unclassified", "_model": model_label}


# ---------------------------------------------------------------------------
# STEP 5: Generate report
# ---------------------------------------------------------------------------

def generate_report(results: list[DemoResult]):
    """Generate CSV and JSON reports."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # JSON report (full detail)
    json_path = OUTPUT_DIR / "demo_report.json"
    json_data = []
    for r in results:
        entry = {
            "name": r.info.name,
            "showcase_url": r.info.showcase_url,
            "demo_url": r.info.demo_iframe_url,
            "live_preview_url": r.info.live_preview_url,
            "category": r.info.category,
            "is_accessible": r.info.is_accessible,
            "is_gated": r.info.is_gated,
            "error": r.info.error,
            "total_steps": r.total_steps_found,
            "steps_captured": r.steps_captured,
            "classification": r.classification,
            "steps": [
                {
                    "step_number": s.step_number,
                    "total_steps": s.total_steps,
                    "tooltip_text": s.tooltip_text,
                    "screenshot": s.screenshot_path,
                }
                for s in r.steps
            ]
        }
        json_data.append(entry)

    with open(json_path, "w") as f:
        json.dump(json_data, f, indent=2)
    print(f"\n📄 JSON report saved: {json_path}")

    # CSV summary
    csv_path = OUTPUT_DIR / "demo_report.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Name", "Category", "Demo URL", "Accessible", "Gated",
            "Total Steps", "Steps Captured", "Classification Type",
            "Overall Score", "Logic Score", "Emotion Score", "Credibility Score",
            "Narrative Flow Score", "Copy Quality Score",
            "Summary", "Strengths", "Weaknesses",
            "Narrative Arc", "Persona Targeting", "Proof Elements",
            "Recommendation", "Error"
        ])
        for r in results:
            cls = r.classification
            writer.writerow([
                r.info.name,
                r.info.category,
                r.info.demo_iframe_url,
                r.info.is_accessible,
                r.info.is_gated,
                r.total_steps_found,
                r.steps_captured,
                cls.get("type", ""),
                cls.get("overall_score", cls.get("score", "")),
                cls.get("logic_score", ""),
                cls.get("emotion_score", ""),
                cls.get("credibility_score", ""),
                cls.get("narrative_flow_score", ""),
                cls.get("copy_quality_score", ""),
                cls.get("summary", ""),
                "; ".join(cls.get("strengths", [])) if isinstance(cls.get("strengths"), list) else cls.get("strengths", ""),
                "; ".join(cls.get("weaknesses", [])) if isinstance(cls.get("weaknesses"), list) else cls.get("weaknesses", ""),
                cls.get("narrative_arc", ""),
                cls.get("persona_targeting", ""),
                cls.get("proof_elements", ""),
                cls.get("recommendation", ""),
                r.info.error,
            ])
    print(f"📊 CSV report saved: {csv_path}")

    # Print summary
    print("\n" + "=" * 70)
    print("📊 SUMMARY")
    print("=" * 70)
    total = len(results)
    accessible = sum(1 for r in results if r.info.is_accessible and not r.info.is_gated)
    gated = sum(1 for r in results if r.info.is_gated)
    classified = sum(1 for r in results if r.classification.get("type"))

    print(f"   Total demos found:  {total}")
    print(f"   Accessible:         {accessible}")
    print(f"   Gated (skipped):    {gated}")
    print(f"   Classified:         {classified}")

    # Type breakdown
    type_counts = {}
    for r in results:
        t = r.classification.get("type", "unprocessed")
        type_counts[t] = type_counts.get(t, 0) + 1
    print(f"\n   Classification breakdown:")
    for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"      {t}: {count}")

    # Top and bottom rated
    scored = [(r, r.classification.get("overall_score", r.classification.get("score", 0))) for r in results if r.classification.get("overall_score") or r.classification.get("score")]
    if scored:
        scored.sort(key=lambda x: -x[1])
        print(f"\n   🏆 Top rated demos:")
        for r, score in scored[:5]:
            print(f"      {score}/10 — {r.info.name} ({r.classification.get('type', '')})")
        print(f"\n   ⚠️  Lowest rated demos:")
        for r, score in scored[-5:]:
            print(f"      {score}/10 — {r.info.name} ({r.classification.get('type', '')})")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    """Convert a name to a safe directory name."""
    return "".join(c if c.isalnum() or c in "-_ " else "" for c in name).strip().replace(" ", "_")[:50]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Storylane Demo Classifier")
    parser.add_argument("--scrape-only", action="store_true", help="Only scrape demo URLs, don't walk or classify")
    parser.add_argument("--no-classify", action="store_true", help="Walk demos but skip classification")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of demos to process")
    parser.add_argument("--demo-url", type=str, help="Process a single demo URL directly")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode (visible)")
    parser.add_argument("--mode", type=str, default="fast", choices=["fast", "full", "smart"],
                        help="Classification mode: fast (text+Haiku, ~$0.10), full (screenshots+Sonnet, ~$5-8), smart (Haiku then Sonnet for top demos, ~$1-2)")
    parser.add_argument("--criteria-file", type=str, default=None,
                        help="Path to a custom classification rubric file (generated from uploaded framework doc)")
    parser.add_argument("--extra-urls", type=str, default=None,
                        help="Comma-separated list of additional demo URLs to process alongside the showcase demos")
    parser.add_argument("--api-key", type=str, default=None,
                        help="Anthropic API key (alternative to ANTHROPIC_API_KEY env var)")
    args = parser.parse_args()

    # Resolve API key: CLI arg > env var
    api_key = args.api_key or ANTHROPIC_API_KEY

    global HEADLESS
    if args.headed:
        HEADLESS = False

    mode_labels = {
        "fast": "⚡ Fast (text-only + Haiku, ~$0.10 for all demos)",
        "full": "🔬 Full (screenshots + Sonnet, ~$5-8 for all demos)",
        "smart": "🧠 Smart (Haiku first, Sonnet for top demos, ~$1-2 for all demos)",
    }

    print("=" * 70)
    print("🎬 STORYLANE DEMO CLASSIFIER")
    print(f"   Mode: {mode_labels.get(args.mode, args.mode)}")
    print("=" * 70)

    # Setup directories
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(viewport=VIEWPORT)
        page = await context.new_page()

        # --- Handle single demo URL mode ---
        if args.demo_url:
            demo = DemoInfo(name="direct-demo", showcase_url="", demo_iframe_url=args.demo_url)
            print(f"\n🎯 Processing single demo: {args.demo_url}")
            result = await walk_demo(page, demo, 0)

            if not args.no_classify and api_key:
                print(f"\n🤖 Classifying demo...")
                result.classification = await classify_demo(result, mode=args.mode, criteria_file=args.criteria_file, api_key=api_key)
                print(f"   Type: {result.classification.get('type', 'unknown')}")
                print(f"   Score: {result.classification.get('score', 'N/A')}")
                print(f"   Summary: {result.classification.get('summary', '')}")

            generate_report([result])
            await browser.close()
            return

        # --- Full pipeline ---

        # Step 1: Scrape showcase page
        demos = await scrape_showcase(page)

        if args.limit > 0:
            demos = demos[:args.limit]
            print(f"\n   ⚡ Limited to first {args.limit} demos")

        if args.scrape_only:
            # Just save the URLs and exit
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            url_path = OUTPUT_DIR / "demo_urls.json"
            with open(url_path, "w") as f:
                json.dump([asdict(d) for d in demos], f, indent=2)
            print(f"\n📄 Demo URLs saved: {url_path}")
            await browser.close()
            return

        # Add any extra custom URLs provided by the user
        if args.extra_urls:
            extra_list = [u.strip() for u in args.extra_urls.split(",") if u.strip()]
            if extra_list:
                print(f"\n   ➕ Adding {len(extra_list)} custom demo URL(s)...")
                for url in extra_list:
                    # Derive a name from the URL
                    name = url.rstrip("/").split("/")[-1] or "custom-demo"
                    name = name.replace("-", " ").replace("_", " ").title()
                    # If it looks like a direct demo URL (contains /demo/), use it directly
                    if "/demo/" in url:
                        demo = DemoInfo(name=f"[Custom] {name}", showcase_url="", demo_iframe_url=url, is_accessible=True)
                    else:
                        # It's a page containing a demo — we'll extract the iframe later
                        demo = DemoInfo(name=f"[Custom] {name}", showcase_url=url)
                    demos.append(demo)
                    print(f"      • {demo.name}: {url}")

        # Step 2: Extract demo iframe URLs from each showcase page
        print(f"\n🔍 STEP 2: Extracting demo URLs from {len(demos)} showcase pages...")
        for i, demo in enumerate(demos):
            print(f"   [{i+1}/{len(demos)}] {demo.name}...", end=" ")
            demo = await extract_demo_url(page, demo)
            if demo.demo_iframe_url:
                print(f"✅ {demo.demo_domain}")
            elif demo.is_gated:
                print(f"🔒 Gated")
            else:
                print(f"❌ {demo.error[:50]}")

        accessible_demos = [d for d in demos if d.is_accessible and d.demo_iframe_url and not d.is_gated]
        print(f"\n   ✅ {len(accessible_demos)} accessible demos out of {len(demos)} total")

        # Step 3: Walk through each demo + classify incrementally
        # Results are saved after each demo so partial progress is preserved if stopped
        print(f"\n🚶 STEP 3: Walking through {len(accessible_demos)} demos...")
        results = []

        # Add inaccessible/gated demos to results upfront
        for demo in demos:
            if not demo.is_accessible or demo.is_gated or not demo.demo_iframe_url:
                r = DemoResult(info=demo)
                if demo.is_gated:
                    r.classification = {"type": "gated", "reason": "Demo requires form submission"}
                else:
                    r.classification = {"type": "inaccessible", "reason": demo.error}
                results.append(r)

        mode = args.mode
        criteria_file = args.criteria_file
        do_classify = not args.no_classify and api_key

        if do_classify:
            mode_label = {"fast": "Fast (Haiku, text-only)", "full": "Full (Sonnet, screenshots)", "smart": "Smart (Haiku first, Sonnet for top demos)"}
            print(f"   Classification mode: {mode_label.get(mode, mode)}")
            if criteria_file:
                print(f"   Using custom rubric: {Path(criteria_file).name}")

        for i, demo in enumerate(accessible_demos):
            print(f"\n   [{i+1}/{len(accessible_demos)}] {demo.name}")
            result = await walk_demo(page, demo, i)

            # Classify immediately after walking (if enabled)
            if do_classify and result.steps:
                print(f"   🤖 Classifying...", end=" ")
                result.classification = await classify_demo(result, mode=mode, criteria_file=criteria_file, api_key=api_key)
                cls_type = result.classification.get("type", "unknown")
                score = result.classification.get("overall_score", result.classification.get("score", "?"))
                print(f"→ {cls_type} (score: {score}/10)")

            results.append(result)

            # Save progress after each demo — partial results are preserved if stopped
            print(f"   💾 Saving progress ({len(results)}/{len(accessible_demos) + len([d for d in demos if not d.is_accessible or d.is_gated or not d.demo_iframe_url])} total)...")
            generate_report(results)

        # Smart mode: re-classify top demos with Sonnet + screenshots
        if do_classify and mode == "smart":
            top_demos = [r for r in results if r.steps and r.classification.get("overall_score", 0) >= 6]
            if top_demos:
                print(f"\n   🔬 Smart mode: Re-classifying {len(top_demos)} top demos with Sonnet + screenshots...")
                for i, result in enumerate(top_demos):
                    print(f"   [{i+1}/{len(top_demos)}] Re-classifying {result.info.name}...", end=" ")
                    result.classification = await classify_demo(result, mode="full", criteria_file=criteria_file, api_key=api_key)
                    cls_type = result.classification.get("type", "unknown")
                    score = result.classification.get("overall_score", result.classification.get("score", "?"))
                    print(f"→ {cls_type} (score: {score}/10)")
                # Save updated classifications
                generate_report(results)
            else:
                print(f"\n   ℹ️  No demos scored 6+ — skipping Sonnet re-classification")

        if not do_classify:
            if not api_key:
                print("\n⚠️  No API key provided — skipping classification")
            print("   Skipping classification step")

        # Final report
        print(f"\n📝 Final report saved.")
        generate_report(results)

        await browser.close()

    print("\n✅ Done!")


if __name__ == "__main__":
    asyncio.run(main())
