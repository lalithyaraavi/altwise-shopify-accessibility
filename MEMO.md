# Memo: Closing the image description gap with Shopify Magic

**To:** Shopify Magic product team
**From:** Lalithya Raavi
**Re:** Evidence for AI image descriptions, and how to test them

## The problem

I audited 422 Shopify stores. The typical store leaves **47% of its product images with no description** (95% CI 42.5 to 50.7%), and another 10% of images have "junk" alt text like file names or internal codes. For a blind shopper using a screen reader, those products have no picture at all.

This is also a merchant risk. Missing image descriptions are one of the most common issues cited in website accessibility lawsuits, and most e-commerce defendants are small businesses, which is Shopify's core merchant base.

## Why a product fix, not a theme fix

Missing alt text is not explained by theme, store size or brand size. In a regression on 390 stores, none of these were significant (all p > 0.4), and together they explained only 9% of the variation. The gap comes from whether each merchant writes descriptions when they add products. A theme update can't fix that. A prompt in the product workflow can.

Paid apps for this already exist in the App Store, yet the gap is still 47%. Most merchants never find or pay for an add-on. That points to a default, built-in feature.

## The proposal

When a merchant uploads a product image, Shopify Magic suggests a description. The merchant can accept, edit or skip it. A one-time bulk option ("42 images have no description. Review suggestions?") covers existing catalogs.

**Approval is required, not optional.** In my evaluation of 100 real undescribed images, AI descriptions were accurate 97% of the time, but 9% had some issue. Errors clustered on busy images: printed text that was skipped, several products summarised instead of named, and a few colour mistakes. So the feature should also flag images with text or several items for a closer look.

I built a working prototype (Ramp) that runs this flow on a real Shopify dev store through the Admin API.

## How to test it

| | |
|---|---|
| **Design** | A/B test, randomised by merchant (a merchant's catalog shouldn't see both versions) |
| **Control** | Current experience, no suggestions |
| **Treatment** | Suggested description on image upload, plus the bulk review prompt |
| **Primary metric** | Share of a merchant's product images with a descriptive alt text after 30 days |
| **Secondary metrics** | Suggestion acceptance rate, edit rate before accepting |
| **Guardrails** | Time to publish a product, rate of merchants dismissing or turning off Magic, reported incorrect descriptions |
| **Sample size** | Store-level standard deviation is about 42 points. To detect a 5-point improvement (80% power, alpha 0.05): about 1,100 merchants per group. A 10-point improvement needs about 275. |
| **Duration** | At least 4 weeks, so results aren't driven by first-week novelty |

**Decision rule, set in advance:** ship if the primary metric improves significantly and no guardrail worsens meaningfully (for example, time to publish rising more than 10%).

**What would change my mind:** if most merchants accept suggestions without reading them and reported errors rise, the approval step isn't working, and the flow needs a stronger review prompt before rollout.

## Limitations

Automated checks catch only some accessibility issues, my store sample leans toward well-designed stores, and the AI evaluation used one rater and one model family.
