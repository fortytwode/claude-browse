# Launch kit

Drafts for the public v1.0 launch. None of this is committed as public
content — it's your personal launch playbook. Read, edit in your voice,
submit when you're ready.

## Files

| File | Purpose |
| ---- | ------- |
| [hn.md](hn.md) | Show HN submission + first comment + follow-up templates |
| [reddit.md](reddit.md) | r/commandline, r/ClaudeAI, r/programming posts |
| [producthunt.md](producthunt.md) | PH listing fields + maker comment |
| [social.md](social.md) | X thread + LinkedIn post |

## Launch order (suggested)

1. **Prereqs:**
   - Demo GIF committed to the repo root as `demo.gif` and linked in README
   - Demo GIF uploaded to the landing page (replaces the placeholder)
   - ConvertKit waitlist form created + embedded on the landing page
   - Landing page published (not draft)
   - PyPI package live (so `pip install claude-browse` actually works)

2. **Launch day:**
   - HN first (Tuesday–Thursday, 7–9am PT)
   - Reddit within a few hours (all three subs, different text each)
   - X thread once HN is live (link to the HN discussion in the last tweet)
   - LinkedIn post late morning PT

3. **Day 2:**
   - ProductHunt (00:01 PT for full 24h window)

4. **Days 2–7:**
   - Reply to every serious comment on HN/Reddit
   - Track GitHub stars/issues daily
   - Convert high-signal feature requests into GitHub issues

## Gating the launch

Don't launch until all 5 of these are true:

- [ ] `pip install claude-browse` works from a fresh env
- [ ] `./install.sh` works on a fresh Mac and a fresh Linux box
- [ ] CI is green on the latest commit
- [ ] Landing page renders correctly on mobile (check on an actual phone)
- [ ] ConvertKit form submits successfully (signup with a throwaway email)

If any of these fail, delay. A soft launch with a broken signup flow is
worse than a delayed launch with everything working.

## Metrics to watch

| Metric | Where | Signal |
| ------ | ----- | ------ |
| GitHub stars | repo page | 50+ in week 1 = good, 200+ = great |
| GitHub issues | repo page | Signal quality > volume |
| PyPI downloads | pypistats.org | 100+ in week 1 = good |
| Waitlist signups | ConvertKit | 50+ in week 1 = good validation |
| Landing-page traffic | GA4 | >1000 in week 1 = HN/PH worked |
| Referrer mix | GA4 | HN-dominant is normal for CLI tools |

## After launch

Week 2: decide whether to start building `claude-sync` based on waitlist
signal. Gate is defined in the main ROADMAP.md (≥500 emails).
