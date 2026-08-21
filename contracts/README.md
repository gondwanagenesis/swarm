# contracts/

Home of capability contracts: each contract = the 8-field spec + a **known-good**
+ a **known-bad** implementation. The gate must accept the first and reject the
second, or it does not deploy (Law 4).

Nothing ships here until M4 lands the integrator. This directory exists so the
shape of the answer is visible from day one.
