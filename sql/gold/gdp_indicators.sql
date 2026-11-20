-- PIB annuel (format long). Entrée : src_world_bank_gdp (tous les lots promus).
-- Dédoublonnage : à clé (country_code, year, indicator_code) égale, le lot le plus récent l'emporte.
SELECT
    country_code,
    country_name,
    CAST(year AS BIGINT) AS year,
    indicator_code,
    CAST(gdp_current_usd AS DOUBLE) AS gdp_current_usd
FROM src_world_bank_gdp
QUALIFY row_number() OVER (
    PARTITION BY country_code, year, indicator_code ORDER BY filename DESC
) = 1
ORDER BY country_code, year, indicator_code
