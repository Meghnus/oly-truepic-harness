# Profile: the only place body-specific values live

`profiles/template.json` ships with every value empty. Copy it, fill it from **your own analysis of your own firmware**, keep it out of
git (`profiles/*` is git-ignored except the template).

```json
{
  "name": "", "isa": "mn10300",
  "image":  {"base": null, "read_floor": null, "file": null},
  "ret_kind": {},
  "disassembler": {"command": [], "line_regex": null, "cache_dir": null},
  "stubs": {}
}
```

| Key | Meaning |
|---|---|
| `image.base` | Address of byte 0 of the decoded main-code image (hex string or int). Required. Find it from the reset head and a few absolute pointers; the loader in your analysis tool knows it. |
| `image.read_floor` | Lowest address at which image bytes read as data (default = base). Use it if the first part of the image is code you never want read as data. |
| `image.file` | The decoded image on disk (relative to the profile file). |
| `ret_kind` | `{"0x...": "ret" \| "retf"}`: how stubbed callees **outside the image** (RAM code, ROM routines) return. Inside the image the exit is read from the callee's code. |
| `disassembler.command` | argv with `{file}` `{wslfile}` `{base}` `{start}` `{end}` placeholders. |
| `disassembler.line_regex` | named groups `addr` `mn` `ops` `bytes`; null = `addr  mnemonic operands ; hexbytes` |
| `disassembler.cache_dir` | decode cache (JSON); local, never publish |
| `stubs` | free-form notes for yourself; the toolkit never reads it |

Unknown keys are errors. `Profile.apply()` lists every required field that is still empty. Set `OMH_PROFILE=path` for tools that call
`profile.from_env()`; the lane exports a spec's `"profile"` as `OMH_PROFILE` to its stages.
