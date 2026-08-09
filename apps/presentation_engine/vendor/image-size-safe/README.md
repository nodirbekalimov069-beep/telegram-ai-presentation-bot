# Fail-closed `image-size` compatibility shim

`pptxgenjs@4.0.1` declares `image-size@^1.2.1`, but its published CommonJS and ESM
bundles do not import or call that package. Every released `image-size` version through
`2.0.2` is affected by the ICNS and JXL/HEIF infinite-loop advisories, and upstream has
not published a patched version.

This local package keeps npm's dependency graph valid without installing any image
parser. Its API always fails closed and advertises no enabled types. The presentation
engine supplies explicit image sizing, rejects ICNS/JXL/HEIF magic bytes at both Python
and Node boundaries, and exercises real image rendering in the renderer test suite.

`npm audit` does not audit the source of local `file:` dependencies. The safety claim for
this shim therefore comes from its two-file reviewed source, the parser-absence regression
test, the existing magic-byte tests, and real PPTX/PDF E2E generation.

Re-audit this shim before changing the pinned `pptxgenjs@4.0.1` version. Remove it when an
upstream release no longer depends on a vulnerable `image-size` package.
