# Launch-hardening review summary

This branch makes the merged storefront state safer to review without pretending unfinished commerce is live.

- ESA remains the primary intended path.
- General Store is relabeled as a preview across source templates and generated pages.
- Buyer-facing false-live labels such as "real checkout," "Add to cart," and "Place order" are replaced with explicit preview language.
- `LAUNCH_AUDIT.md` records Keep, Improve, Trim, Hide / relabel, remaining blockers, launch gates, and the current recommendation.
- README files now state the true launch status.

Validation:

- General Store source templates match generated HTML after cross-link resolution.
- Forbidden buyer-facing labels are absent from the three General Store source and generated pages.
- ESA navigation/footer labels match in source and generated pages.

Recommendation: hold public launch, complete the ESA gates, then launch ESA only. Keep General Store outside the buyer path until real retail operations are connected.
