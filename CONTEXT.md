# KFCQuant Domain Language

KFCQuant is a personal, auditable A-share research system with a separate operational control plane. Research decisions and service operations are distinct concerns and must remain independently observable.

## Research

**Research Service**:
The service that produces time-bounded stock opportunity signals, maintains the paper portfolio, and publishes research results. It never deploys software or executes real brokerage orders.
_Avoid_: Stock Picker, Trading Bot

**Signal Run**:
One auditable evaluation performed for a defined market session, information cutoff, and strategy version.
_Avoid_: Recommendation, Prediction

**Morning Watchlist**:
The 08:30 signal run that identifies stocks and conditions worth observing before the market opens. It does not create a paper buy order.
_Avoid_: Morning Buy List

**Pre-close Entry List**:
The 14:40 signal run that ranks possible pre-close entries using information available by its cutoff and may create paper orders when all safety gates pass.
_Avoid_: Tomorrow Winners

## Operations

**Operations Manager**:
The operator-facing control plane that observes and manages releases and runtime state of the Research Service. It cannot calculate, edit, or approve research signals.
_Avoid_: Recommendation Manager, Admin Script

**Release**:
An immutable, tested build of the Research Service identified by a version and source revision.
_Avoid_: Latest Code, Git Pull

**Deployment**:
The controlled promotion of a Release to the server, including verification and the ability to roll back to the previously active Release.
_Avoid_: Update, Restart

**Active Release**:
The Release currently serving the web interface and scheduled research workload.
_Avoid_: Current Code

**Runtime State**:
The observed health of the Active Release, its web process, scheduler, dependencies, and latest job executions. Runtime State is separate from deployment progress.
_Avoid_: Update Status
