-- =====================================================================
-- STEP 3: MEASURE THE ISSUES                          (BigQuery SQL)
-- Run each query separately: select it in the editor, then click Run.
-- All tables live in  shopify-accessibility-515103.audit
-- =====================================================================


-- ---------------------------------------------------------------------
-- 3.0  Check the refreshed tables (run after re-uploading)
-- ---------------------------------------------------------------------
SELECT sample_group, status = 'ok' AS got_data, COUNT(*) AS n_stores
FROM `shopify-accessibility-515103.audit.stores`
GROUP BY 1, 2
ORDER BY 1, 2;


-- ---------------------------------------------------------------------
-- 3.1  METRIC DESIGN: classify every image's alt text  (creates a VIEW)
-- A view is a saved query you can use like a table.
--   missing     = no alt text at all (screen reader says nothing useful)
--   junk        = text that isn't a description: file names, internal codes
--                 ("PDP2 Option[...] MAIN", "Color:Ocean;"), slugs, numbers,
--                 placeholder words ("featured", "slideshow")
--   minimal     = only the product title, or 3 words or fewer ("Jet Black")
--   descriptive = 4+ words that actually describe the image
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW `shopify-accessibility-515103.audit.images_classified` AS
WITH prep AS (
  SELECT
    *,
    TRIM(COALESCE(alt, ''))                      AS a,   -- NULL -> '' so empty and NULL are treated the same
    LOWER(TRIM(COALESCE(alt, '')))               AS al,
    LOWER(TRIM(COALESCE(product_title, '')))     AS tl,
    ARRAY_LENGTH(REGEXP_EXTRACT_ALL(COALESCE(alt, ''), r'[A-Za-z]+')) AS n_words
  FROM `shopify-accessibility-515103.audit.images_alt`
)
SELECT
  * EXCEPT (a, al, tl),
  CASE
    WHEN a = '' THEN 'missing'
    WHEN REGEXP_CONTAINS(al, r'\.(jpe?g|png|webp|gif|avif)$')                                  -- a file name
      OR REGEXP_CONTAINS(al, r'(::|\|\||option\[|^img[_-]?\d|^dsc|pdp\d?|_main\b|^colou?r:|^size:|#colou?r_)')  -- internal codes
      OR REGEXP_CONTAINS(al, r'^(featured|slideshow|image|img|photo|picture|thumbnail|banner|default|product)( ?\d+)?$')
      OR NOT REGEXP_CONTAINS(a, r'[A-Za-z]{3}')                                                -- no real word ("11", "V076-02-0000")
      OR (NOT REGEXP_CONTAINS(a, r'\s') AND REGEXP_CONTAINS(a, r'[-_]'))                       -- a slug ("natural-diamond")
      THEN 'junk'
    WHEN al = tl OR n_words <= 3 OR STRPOS(tl, al) > 0 THEN 'minimal'
    ELSE 'descriptive'
  END AS alt_class
FROM prep;


-- ---------------------------------------------------------------------
-- 3.2  HEADLINE (image level): what share of images falls in each class?
-- SUM(...) OVER () is a WINDOW FUNCTION: it adds up all rows without
-- collapsing them, so each row can be divided by the grand total.
-- ---------------------------------------------------------------------
SELECT
  alt_class,
  COUNT(*)                                              AS n_images,
  ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1)      AS pct_of_images
FROM `shopify-accessibility-515103.audit.images_classified`
GROUP BY alt_class
ORDER BY n_images DESC;


-- ---------------------------------------------------------------------
-- 3.3  HEADLINE (store level) with a 95% CONFIDENCE INTERVAL
-- Images from the same store aren't independent (one team writes them all),
-- so the honest unit is the STORE: compute each store's % missing, then
-- average across stores. CI = mean +/- 1.96 * standard error.
-- ---------------------------------------------------------------------
WITH per_store AS (                                   -- a CTE: a named step inside one query
  SELECT
    domain,
    COUNT(*)                                         AS n_images,
    AVG(IF(alt_class = 'missing', 1, 0))             AS share_missing,
    AVG(IF(alt_class IN ('missing', 'junk'), 1, 0))  AS share_unusable
  FROM `shopify-accessibility-515103.audit.images_classified`
  GROUP BY domain
)
SELECT
  COUNT(*)                                                             AS n_stores,
  ROUND(100 * AVG(share_missing), 1)                                   AS avg_pct_missing,
  ROUND(100 * (AVG(share_missing) - 1.96 * STDDEV_SAMP(share_missing) / SQRT(COUNT(*))), 1) AS ci_low,
  ROUND(100 * (AVG(share_missing) + 1.96 * STDDEV_SAMP(share_missing) / SQRT(COUNT(*))), 1) AS ci_high,
  ROUND(100 * AVG(share_unusable), 1)                                  AS avg_pct_missing_or_junk,
  ROUND(100 * COUNTIF(share_missing = 0) / COUNT(*), 1)                AS pct_stores_with_none_missing,
  ROUND(100 * COUNTIF(share_missing >= 0.5) / COUNT(*), 1)             AS pct_stores_half_or_more_missing
FROM per_store;


-- ---------------------------------------------------------------------
-- 3.4  WINDOW FUNCTIONS: rank stores within their category
-- RANK() OVER (PARTITION BY category ...) restarts the ranking for each category.
-- ---------------------------------------------------------------------
WITH per_store AS (
  SELECT i.domain, s.category, s.sample_group,
         ROUND(100 * AVG(IF(i.alt_class = 'missing', 1, 0)), 1) AS pct_missing
  FROM `shopify-accessibility-515103.audit.images_classified` i
  JOIN `shopify-accessibility-515103.audit.stores` s USING (domain)        -- JOIN: bring in each store's category
  GROUP BY 1, 2, 3
)
SELECT
  category, domain, sample_group, pct_missing,
  RANK() OVER (PARTITION BY category ORDER BY pct_missing DESC)            AS worst_in_category,
  ROUND(AVG(pct_missing) OVER (PARTITION BY category), 1)                  AS category_avg
FROM per_store
ORDER BY category, worst_in_category;


-- ---------------------------------------------------------------------
-- 3.5  PAGE CHECKS: % of pages failing each rule, and WHO it affects
-- The denominator comes from `pages` (every page checked, including
-- pages with zero issues). Without it, the percentages would be wrong.
-- ---------------------------------------------------------------------
WITH denom AS (
  SELECT page_type, COUNT(*) AS n_pages
  FROM `shopify-accessibility-515103.audit.pages`
  GROUP BY page_type
),
issues AS (
  SELECT
    page_type, rule_id, domain, impact,
    CASE
      WHEN rule_id = 'color-contrast'                                  THEN 'Low vision / older shoppers'
      WHEN rule_id IN ('image-alt', 'role-img-alt', 'svg-img-alt',
                       'input-image-alt', 'area-alt')                  THEN 'Blind (screen reader)'
      WHEN rule_id IN ('button-name', 'link-name', 'label', 'select-name',
                       'aria-input-field-name', 'aria-command-name',
                       'frame-title', 'document-title', 'html-has-lang',
                       'list', 'listitem', 'heading-order',
                       'aria-allowed-attr', 'aria-required-attr',
                       'aria-valid-attr-value', 'aria-hidden-focus')    THEN 'Blind (screen reader)'
      WHEN rule_id = 'label-content-name-mismatch'                     THEN 'Voice control users'
      WHEN rule_id IN ('target-size', 'scrollable-region-focusable',
                       'nested-interactive')                           THEN 'Motor / keyboard users'
      WHEN rule_id IN ('meta-viewport', 'meta-viewport-large')         THEN 'Low vision (cannot zoom)'
      ELSE 'Other'
    END AS who_affected
  FROM `shopify-accessibility-515103.audit.page_issues`
)
SELECT
  i.page_type, i.rule_id, i.who_affected,
  COUNT(DISTINCT i.domain)                                   AS n_pages_failing,
  d.n_pages,
  ROUND(100 * COUNT(DISTINCT i.domain) / d.n_pages, 1)       AS pct_pages_failing
FROM issues i
JOIN denom d USING (page_type)
GROUP BY 1, 2, 3, 5
ORDER BY i.page_type, pct_pages_failing DESC;


-- ---------------------------------------------------------------------
-- 3.6  ROLL-UP BY GROUP: % of stores with at least one issue affecting each group
-- (on either page). Copy the CASE from 3.5 (or turn 3.5's CTE into a view).
-- ---------------------------------------------------------------------
WITH checked AS (
  SELECT COUNT(DISTINCT domain) AS n_stores FROM `shopify-accessibility-515103.audit.pages`
),
issues AS (
  SELECT domain,
    CASE
      WHEN rule_id = 'color-contrast' THEN 'Low vision / older shoppers'
      WHEN rule_id = 'label-content-name-mismatch' THEN 'Voice control users'
      WHEN rule_id IN ('target-size', 'scrollable-region-focusable', 'nested-interactive') THEN 'Motor / keyboard users'
      WHEN rule_id IN ('meta-viewport', 'meta-viewport-large') THEN 'Low vision (cannot zoom)'
      ELSE 'Blind (screen reader)'
    END AS who_affected
  FROM `shopify-accessibility-515103.audit.page_issues`
)
SELECT who_affected,
       COUNT(DISTINCT domain) AS n_stores,
       ROUND(100 * COUNT(DISTINCT domain) / ANY_VALUE(c.n_stores), 1) AS pct_of_stores
FROM issues CROSS JOIN checked c
GROUP BY who_affected
ORDER BY pct_of_stores DESC;


-- ---------------------------------------------------------------------
-- 3.7  READABILITY of product descriptions (cognitive accessibility)
-- Flesch reading ease: 60+ = plain English, 30-60 = fairly hard, <30 = very hard.
-- Placeholders and very short descriptions (no score) are excluded.
-- ---------------------------------------------------------------------
SELECT
  CASE WHEN desc_reading_ease >= 60 THEN '1. plain (60+)'
       WHEN desc_reading_ease >= 30 THEN '2. fairly hard (30-60)'
       ELSE '3. very hard (<30)' END              AS readability,
  COUNT(*)                                        AS n_products,
  ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM `shopify-accessibility-515103.audit.products`
WHERE is_placeholder = 0 AND desc_reading_ease IS NOT NULL
GROUP BY readability
ORDER BY readability;


-- ---------------------------------------------------------------------
-- 3.8  NULL CHECK (a classic interview question): how many NULLs per column?
-- ---------------------------------------------------------------------
SELECT
  COUNT(*)                         AS n_rows,
  COUNTIF(alt IS NULL)             AS alt_null,
  COUNTIF(TRIM(alt) = '')          AS alt_empty_string,
  COUNTIF(product_type IS NULL)    AS product_type_null
FROM `shopify-accessibility-515103.audit.images_alt`;
