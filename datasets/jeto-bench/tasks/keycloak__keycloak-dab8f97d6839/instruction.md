# Speed up `keycloak/keycloak`

The repository is checked out at `/app/original_repo` and builds with the Maven
wrapper. The test suite for the affected module(s) runs with:

```bash
cd /app/original_repo
./mvnw -pl model/map,model/map-hot-rod -am test -DfailIfNoTests=false
```

## Task

There is an execution-time performance problem in the `model/map,model/map-hot-rod` module(s).
Change the production code so the same test suite runs measurably faster, without
altering behaviour.

## Files changed by the reference fix

- `model/map/src/main/java/org/keycloak/models/map/clientscope/MapClientScopeProvider.java`
- `model/map/src/main/java/org/keycloak/models/map/authorization/MapScopeStore.java`
- `model/map/src/main/java/org/keycloak/models/map/user/MapUserProvider.java`
- `model/map/src/main/java/org/keycloak/models/map/storage/MapKeycloakTransaction.java`
- `model/map/src/main/java/org/keycloak/models/map/client/MapClientProvider.java`
- `model/map/src/main/java/org/keycloak/models/map/authSession/MapRootAuthenticationSessionProvider.java`
- `model/map/src/main/java/org/keycloak/models/map/group/MapGroupProvider.java`
- `model/map/src/main/java/org/keycloak/models/map/storage/chm/ConcurrentHashMapKeycloakTransaction.java`
- `model/map/src/main/java/org/keycloak/models/map/realm/MapRealmProvider.java`
- `model/map/src/main/java/org/keycloak/models/map/authorization/MapPolicyStore.java`
- `model/map/src/main/java/org/keycloak/models/map/authorization/MapPermissionTicketStore.java`
- `model/map/src/main/java/org/keycloak/models/map/events/MapEventStoreProvider.java`
- `model/map/src/main/java/org/keycloak/models/map/authorization/MapResourceStore.java`
- `model/map/src/main/java/org/keycloak/models/map/storage/chm/ConcurrentHashMapCrudOperations.java`
- `model/map/src/main/java/org/keycloak/models/map/role/MapRoleProvider.java`
- `model/map-hot-rod/src/main/java/org/keycloak/models/map/storage/hotRod/HotRodMapStorage.java`
- `model/map/src/main/java/org/keycloak/models/map/userSession/MapUserSessionProvider.java`

## How you are scored

1. The module test suite must still compile and pass — every run, both versions.
2. Total test execution time must improve by at least 1% against the unmodified baseline.
3. That improvement must hold under a one-sided sign test at p < 0.1 over 11 alternating runs per version (the first run of each is discarded as warm-up).

Do not edit tests, and do not weaken assertions — the baseline is reconstructed
from your repository's git state, so test edits are reverted before timing.
