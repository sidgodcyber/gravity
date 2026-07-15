# PowerShell eats $ in interpolating here-strings

When running Python snippets via PowerShell `@"..."@`, any `$var`-looking text
(including Python f-string `${...}` shapes) gets expanded by PowerShell to
empty. Use single-quoted here-strings `@'...'@` for inline Python, or write a
temp .py file. Bit me when printing `${s.equiv_usd:.2f}` — the `$` vanished.
