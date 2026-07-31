# Speed up `keycloak/keycloak`

The repository is checked out at `/app/original_repo` and builds with the Maven
wrapper. The test suite for the affected module(s) runs with:

```bash
cd /app/original_repo
./mvnw -pl saml-core,server-spi -am test -DfailIfNoTests=false
```

## Task

There is an execution-time performance problem in the `saml-core,server-spi` module(s).
Change the production code so the same test suite runs measurably faster, without
altering behaviour.

## Files changed by the reference fix

- `saml-core/src/main/java/org/keycloak/saml/processing/core/saml/v2/util/XMLTimeUtil.java`
- `saml-core/src/main/java/org/keycloak/saml/processing/core/parsers/saml/metadata/SAMLEncryptionMethodParser.java`
- `server-spi/src/main/java/org/keycloak/component/PrioritizedComponentModel.java`
- `server-spi/src/main/java/org/keycloak/component/ComponentModel.java`

## How you are scored

1. The module test suite must still compile and pass — every run, both versions.
2. Total test execution time must improve by at least 1% against the unmodified baseline.
3. That improvement must hold under a one-sided sign test at p < 0.1 over 11 alternating runs per version (the first run of each is discarded as warm-up).

Do not edit tests, and do not weaken assertions — the baseline is reconstructed
from your repository's git state, so test edits are reverted before timing.
