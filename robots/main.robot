*** Settings ***
Library    ai_rpa.robot_library.AiRpaLibrary

*** Variables ***
${WORKFLOW}    ${CURDIR}/../workflows/dianxiaomi_draft_demo.json

*** Tasks ***
Run AI RPA Workflow
    Run Workflow    ${WORKFLOW}

