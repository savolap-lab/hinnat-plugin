---
name: hinnat
description: Find the cheapest electrical wholesale products (Onninen, Sonepar, Ahlsell) and produce an Excel quote. Use when asked the price of a product (kaapeli, keskus, komponentti, tarvike), given a BOM or material list to price, asked which supplier to order from or whether to split an order, given a new supplier price list, or taught a site word, a site rule or a buying rule.
---

# Hinnat

Every command is the same launcher with the plugin's own Python (nothing is
installed on this machine, Windows or Mac) and the data folder written out in
full:

```bash
"${CLAUDE_PLUGIN_ROOT}/python/python" "${CLAUDE_PLUGIN_ROOT}/scripts/hinnat.py" --data "${CLAUDE_PLUGIN_DATA}" <command> ...
```

The same line works in Bash on Windows and macOS. In PowerShell put `&` in
front. Below and in the instructions, `hinnat` stands for that whole prefix. Run it in this conversation's folder: the quote, the
order files and the list are saved in `tarjoukset/` there. Write the list CSV
there too (`tarjoukset/lista.csv`).
The organisation's rules are in `${CLAUDE_PLUGIN_DATA}/rules/` and this user's
own in `${CLAUDE_PLUGIN_DATA}/me/`.

**The detailed instructions come from the server** with the organisation key:
the session-start hook printed them between the lines `=== HINNAT-OHJE ===`
and `=== /HINNAT-OHJE ===`. Follow them. If they are not in this conversation,
run `hinnat ohje` and follow what it prints.

Run `hinnat bom` with the tool timeout at 600000 ms, then send the Excel and
every order file (the Sonepar CSV) as attachments with `SendUserFile` —
never as links. Never invent a price, and
never ask for the organisation key in the chat — `hinnat avain` opens the window
for it.
