#!/usr/bin/env bash

echo "-------------------------------"
echo "-> Executing UT"
echo "-------------------------------"
RESULT=""
if ! unit_test_executor; then
    RESULT="FAIL"
fi
echo "-------------------------------"
echo "-> Finished UT ${RESULT}"
echo "-------------------------------"