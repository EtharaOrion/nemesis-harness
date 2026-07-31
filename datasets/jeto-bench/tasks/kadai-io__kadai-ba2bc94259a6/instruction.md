# Speed up `kadai-io/kadai`

The repository is checked out at `/app/original_repo` and builds with the Maven
wrapper. The test suite for the affected module(s) runs with:

```bash
cd /app/original_repo
./mvnw -pl history/kadai-simplehistory-provider,lib/kadai-core -am test -DfailIfNoTests=false
```

## Task

There is an execution-time performance problem in the `history/kadai-simplehistory-provider,lib/kadai-core` module(s).
Change the production code so the same test suite runs measurably faster, without
altering behaviour.

## Files changed by the reference fix

- `lib/kadai-core/src/main/java/io/kadai/user/internal/UserQuerySqlProvider.java`
- `history/kadai-simplehistory-provider/src/main/java/io/kadai/simplehistory/impl/workbasket/WorkbasketHistoryQueryMapper.java`
- `lib/kadai-core/src/main/java/io/kadai/workbasket/internal/WorkbasketQueryMapper.java`
- `lib/kadai-core/src/main/java/io/kadai/task/internal/TaskCommentQuerySqlProvider.java`
- `lib/kadai-core/src/main/java/io/kadai/classification/internal/ClassificationQueryMapper.java`
- `history/kadai-simplehistory-provider/src/main/java/io/kadai/simplehistory/impl/classification/ClassificationHistoryQueryMapper.java`
- `lib/kadai-core/src/main/java/io/kadai/task/internal/AttachmentMapper.java`
- `history/kadai-simplehistory-provider/src/main/java/io/kadai/simplehistory/impl/task/TaskHistoryQueryMapper.java`

## How you are scored

1. The module test suite must still compile and pass — every run, both versions.
2. Total test execution time must improve by at least 1% against the unmodified baseline.
3. That improvement must hold under a one-sided sign test at p < 0.1 over 11 alternating runs per version (the first run of each is discarded as warm-up).

Do not edit tests, and do not weaken assertions — the baseline is reconstructed
from your repository's git state, so test edits are reverted before timing.
