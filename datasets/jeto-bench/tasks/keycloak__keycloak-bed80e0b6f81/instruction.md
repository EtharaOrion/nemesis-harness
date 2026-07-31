# Speed up `keycloak/keycloak`

The repository is checked out at `/app/original_repo` and builds with the Maven
wrapper. The test suite for the affected module(s) runs with:

```bash
cd /app/original_repo
./mvnw -pl model/map-hot-rod -am test -DfailIfNoTests=false
```

## Task

There is an execution-time performance problem in the `model/map-hot-rod` module(s).
Change the production code so the same test suite runs measurably faster, without
altering behaviour.

## Files changed by the reference fix

- `model/map-hot-rod/src/main/java/org/keycloak/models/map/storage/hotRod/group/HotRodGroupEntity.java`
- `model/map-hot-rod/src/main/java/org/keycloak/models/map/storage/hotRod/userSession/HotRodUserSessionEntity.java`
- `model/map-hot-rod/src/main/java/org/keycloak/models/map/storage/hotRod/user/HotRodUserEntity.java`
- `model/map-hot-rod/src/main/java/org/keycloak/models/map/storage/hotRod/role/HotRodRoleEntity.java`
- `model/map-hot-rod/src/main/java/org/keycloak/models/map/storage/hotRod/authSession/HotRodRootAuthenticationSessionEntity.java`
- `quarkus/runtime/src/main/java/org/keycloak/quarkus/runtime/themes/FlatClasspathThemeResourceProviderFactory.java`
- `services/src/main/java/org/keycloak/theme/ClasspathThemeResourceProviderFactory.java`
- `model/map-hot-rod/src/main/java/org/keycloak/models/map/storage/hotRod/userSession/HotRodAuthenticatedClientSessionEntity.java`
- `model/map-hot-rod/src/main/java/org/keycloak/models/map/storage/hotRod/loginFailure/HotRodUserLoginFailureEntity.java`
- `model/map-hot-rod/src/main/java/org/keycloak/models/map/storage/hotRod/realm/HotRodRealmEntity.java`
- `model/map-hot-rod/src/main/java/org/keycloak/models/map/storage/hotRod/clientscope/HotRodClientScopeEntity.java`
- `model/map-hot-rod/src/main/java/org/keycloak/models/map/storage/hotRod/client/HotRodClientEntity.java`

## How you are scored

1. The module test suite must still compile and pass — every run, both versions.
2. Total test execution time must improve by at least 1% against the unmodified baseline.
3. That improvement must hold under a one-sided sign test at p < 0.1 over 11 alternating runs per version (the first run of each is discarded as warm-up).

Do not edit tests, and do not weaken assertions — the baseline is reconstructed
from your repository's git state, so test edits are reverted before timing.
