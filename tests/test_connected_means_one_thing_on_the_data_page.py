"""NC-207. The word "connected" means one thing on the Data page.

The top and the bottom counters of the Data page must not use the word
"connected" for two different measures. The top ("N/M sources connected") counts
sources that have brought data in; the bottom counts sources that need a setting
and have it, so WhatsApp, iMessage and Contacts, which need none, could never
count there however much they brought.

The bottom therefore says "set up", which is what it measures and matches the two
counts beside it. "Connected" is left to the top, where it means what a person
means by it.
"""

# NC-236: all three checks in this file went with the old Data page. They pinned
# two counters, "N/M sources connected" at the top and "N set up" at the bottom,
# so that the word "connected" meant one thing. The new Data page has neither
# counter: every source is its own row with a state dot, and there is no total to
# word. Nothing on it counts sources as "connected", so there is no second meaning
# to keep apart. The file is kept, empty, so that references to it keep working.
