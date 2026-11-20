# 📄 Memo: Closing the Image Description Gap with Shopify Magic

| | |
|---|---|
| **To** | Shopify Magic product team |
| **From** | Lalithya Raavi |
| **Re** | Evidence for AI image descriptions, and how to test them |

---

## 📌 TL;DR

> - The typical Shopify store leaves **47% of its product images undescribed**.
> - It's a **merchant habit, not a theme problem**, so the fix belongs in the product workflow.
> - **Recommendation:** Shopify Magic suggests an image description when a merchant uploads a product image, and the merchant approves it.
> - AI descriptions were **97% accurate** on 100 real images, so suggest-and-approve is safe; auto-publishing is not.
> - Test it with an A/B test analysed with **CUPED**: about 1,000 merchants per group.

---

## 🔍 The problem

I audited 422 Shopify stores:

- The typical store leaves **47%** of product images with no description *(95% CI: 42.5 to 50.7%)*.
- Another **10%** of images have junk alt text, like file names or internal codes.
- Without descriptions, products have no picture for screen-reader users, and are harder for search engines and AI shopping agents to understand.

---

## 💼 Why this matters to Shopify

- ⚖️ **Merchant retention:** most e-commerce accessibility lawsuits target small businesses, Shopify's core base. A lawsuit can push a merchant off the platform.
- 🇪🇺 **International sales:** the European Accessibility Act has been enforced since June 2025.
- 🤖 **AI shopping agents:** agents rely on structured product information. Undescribed images are close to invisible to them.
- 🛒 **More buyers:** every shopper who can't use a store is a lost sale.

*I don't claim this lifts sales. The test below is designed to measure that.*

---

## 🧩 Why a product fix, not a theme fix

- In a regression on 390 stores, **theme, store size and brand size were not significant** *(all p > 0.4)*, and together explained only **9%** of the variation.
- The gap comes from whether each merchant writes descriptions when adding products. **A theme update can't fix that. A prompt in the product workflow can.**
- Paid alt-text apps already exist, yet the gap is still 47%. Most merchants never find or pay for an add-on, which points to a **built-in default**.

---

## ✨ The proposal

1. **On upload:** when a merchant adds a product image, Shopify Magic suggests a description. The merchant can accept, edit or skip it.
2. **For existing catalogs:** a one-time prompt, *"42 images have no description. Review suggestions?"*
3. **Extra care on busy images:** flag images with printed text or several products for a closer look.

**Why approval is required:** on 100 real undescribed images, AI descriptions were **97% accurate**, but **9%** had some issue: skipped printed text, several products summarised instead of named, and a few colour mistakes.

🛠️ **Prototype:** I built **Ramp**, which runs this flow on a real Shopify dev store through the Admin API.

---

## 🧪 How to test it

#### Setup
| | |
|---|---|
| **Design** | A/B test, randomised by merchant |
| **Control** | Current experience, no suggestions |
| **Treatment** | Suggested descriptions on upload, plus the bulk review prompt |
| **Duration** | At least 4 weeks, so results aren't driven by novelty |

#### Metrics
| Type | Metric |
|---|---|
| 🎯 **Primary** | Share of a merchant's images with descriptive alt text after 30 days |
| 💰 **Business** | Conversion rate on fixed products; traffic from search and AI shopping agents |
| 📈 **Secondary** | Suggestion acceptance rate; edit rate before accepting |
| 🛡️ **Guardrails** | Time to publish a product; merchants turning off Magic; reported wrong descriptions |

#### Sample size, checked by simulation
- **Baseline:** 39% of a typical store's images are descriptive, with a huge spread between stores *(SD 40 points)*.
- **Standard formula:** about **1,000 merchants per group** to detect a 5-point lift.
- **Simulation** *(1,000 experiments built from the real store data)*:

| | Plain t-test | CUPED |
|---|---|---|
| Detects a real 5-point lift | 62% | **100%** |
| False alarms when there's no effect | 6% | 6% |

**So the test should use CUPED:** adjust for each merchant's coverage before the test, which removes the spread between stores. *(Notebook: `analysis/step5_ab_test.ipynb`)*

#### Decisions, set in advance
- ✅ **Ship if:** the primary metric improves significantly and no guardrail worsens meaningfully (e.g. time to publish up more than 10%).
- 🔄 **What would change my mind:** if merchants accept suggestions without reading them and reported errors rise, the approval step isn't working and needs a stronger review prompt.

---

## ⚠️ Limitations

- Automated checks catch only some accessibility issues.
- My store sample leans toward well-designed stores.
- The AI evaluation used one rater and one model family.
