# Licence texts

This folder holds one licence text per component bundled in the release zip:
`licenses/<component>.txt`, for **the exact version in that release**.

In the repository it contains only this file and `INVENTORY.md`. The texts are
generated on the build computer, because they must match the packages in the
build:

```
powershell -ExecutionPolicy Bypass -File setup.ps1 -Licenses
```

This fills the folder and regenerates `INVENTORY.md` from each installed
package's own metadata. `build.ps1` only warns if the folder is empty, so that
test builds work. The release checklist in RELEASING.md makes sure a published
zip has the texts.
