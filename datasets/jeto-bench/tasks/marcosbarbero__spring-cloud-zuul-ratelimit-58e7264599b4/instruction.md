# Speed up `marcosbarbero/spring-cloud-zuul-ratelimit`

The repository is checked out at `/app/original_repo` and builds with the Maven
wrapper. The test suite for the affected module(s) runs with:

```bash
cd /app/original_repo
./mvnw -pl spring-cloud-zuul-ratelimit-core -am test -DfailIfNoTests=false
```

## Task

There is an execution-time performance problem in the `spring-cloud-zuul-ratelimit-core` module(s).
Change the production code so the same test suite runs measurably faster, without
altering behaviour.

## Files changed by the reference fix

- `spring-cloud-zuul-ratelimit-core/src/main/java/com/marcosbarbero/cloud/autoconfigure/zuul/ratelimit/config/repository/RedisRateLimiter.java`
- `spring-cloud-zuul-ratelimit-core/src/main/java/com/marcosbarbero/cloud/autoconfigure/zuul/ratelimit/filters/RateLimitPreFilter.java`
- `spring-cloud-zuul-ratelimit-core/src/main/java/com/marcosbarbero/cloud/autoconfigure/zuul/ratelimit/config/properties/RateLimitProperties.java`
- `spring-cloud-zuul-ratelimit-core/src/main/java/com/marcosbarbero/cloud/autoconfigure/zuul/ratelimit/support/StringToMatchTypeConverter.java`
- `spring-cloud-zuul-ratelimit-core/src/main/java/com/marcosbarbero/cloud/autoconfigure/zuul/ratelimit/RateLimitAutoConfiguration.java`
- `spring-cloud-zuul-ratelimit-core/src/main/java/com/marcosbarbero/cloud/autoconfigure/zuul/ratelimit/support/DefaultRateLimitUtils.java`
- `spring-cloud-zuul-ratelimit-core/src/main/java/com/marcosbarbero/cloud/autoconfigure/zuul/ratelimit/support/RateLimitConstants.java`
- `spring-cloud-zuul-ratelimit-core/src/main/java/com/marcosbarbero/cloud/autoconfigure/zuul/ratelimit/filters/AbstractRateLimitFilter.java`

## How you are scored

1. The module test suite must still compile and pass — every run, both versions.
2. Total test execution time must improve by at least 1% against the unmodified baseline.
3. That improvement must hold under a one-sided sign test at p < 0.1 over 11 alternating runs per version (the first run of each is discarded as warm-up).

Do not edit tests, and do not weaken assertions — the baseline is reconstructed
from your repository's git state, so test edits are reverted before timing.
