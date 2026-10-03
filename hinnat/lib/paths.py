"""Where the code is and where the data is.

Two ways to run the same code:

  * repo clone (the original model): code and data share one folder. Prices
    in dist/, the buyers' rules in rules/, credentials in .env and .secrets/.
  * plugin: the code sits in the plugin cache, which Claude Code wipes on
    every update, so everything that changes lives in HINNAT_DATA
    (= ${CLAUDE_PLUGIN_DATA}): prices.sqlite, rules/ from the server, me/,
    tarjoukset/, outbox/, and .env/.secrets/ for a buyer's own Onninen login.

scripts/hinnat.py sets HINNAT_DATA before importing anything here; without it
every path below is exactly what it was before the plugin existed.

What always comes from the code: dist/rules.json (built from
rules/filters.yaml), rules/suppliers.yaml and the general glossary
rules/sanasto-yleinen.yaml.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("HINNAT_DATA") or ""
PLUGIN = bool(DATA)


def data(*parts):
    """A path under the data folder: HINNAT_DATA in a plugin, the repo otherwise."""
    return os.path.join(DATA or ROOT, *parts)


PRICES_DB = data("prices.sqlite") if PLUGIN else os.path.join(ROOT, "dist", "prices.sqlite")
RULES_JSON = os.path.join(ROOT, "dist", "rules.json")

# The organisation's rules: preferences.md, kohteet.md, sanasto.yaml.
ORG_RULES = data("rules")
# A buyer's own rules, plugin only — never sent anywhere.
ME = data("me")

# Glossaries, first match wins: the organisation's own words beat the general
# slang that ships with the plugin, so a customer can redefine a word.
# In a clone both files sit in rules/; the general one is what the plugin ships.
SANASTO = [os.path.join(ORG_RULES, "sanasto.yaml"),
           os.path.join(ROOT, "rules", "sanasto-yleinen.yaml")]

ENV_FILE = data(".env")
SECRETS_DIR = data(".secrets")
QUOTES = data("tarjoukset")
OUTBOX = data("outbox")
