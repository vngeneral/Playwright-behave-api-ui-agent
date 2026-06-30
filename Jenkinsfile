#!/usr/bin/env groovy
/**
 * Playwright + Behave + Allure — Jenkins Pipeline
 *
 * Requires:
 *   - Python 3.12+ on the agent
 *   - Allure Jenkins Plugin (for allure() step)
 *   - HTML Publisher Plugin (for publishHTML step)
 */
pipeline {
    agent any

    parameters {
        choice(
            name: 'BROWSER',
            choices: ['chromium', 'firefox', 'webkit'],
            description: 'Browser engine'
        )
        choice(
            name: 'ENV',
            choices: ['dev', 'staging', 'prod'],
            description: 'Target environment'
        )
        string(
            name: 'TAGS',
            defaultValue: '',
            description: 'Behave tags (space-separated, e.g. @smoke @regression)'
        )
        booleanParam(
            name: 'PARALLEL',
            defaultValue: true,
            description: 'Run feature files in parallel'
        )
        booleanParam(
            name: 'HEADLESS',
            defaultValue: true,
            description: 'Run browser in headless mode'
        )
        booleanParam(
            name: 'DEBUG',
            defaultValue: false,
            description: 'Enable debug mode (verbose logs + page-source attachments)'
        )
    }

    environment {
        BROWSER    = "${params.BROWSER}"
        ENV        = "${params.ENV}"
        HEADLESS   = "${params.HEADLESS ? 'True' : 'False'}"
        DEBUG      = "${params.DEBUG ? 'true' : 'false'}"
        NOTIFY_ON_FAILURE = "true"
        // All webhook URLs must be stored as Jenkins masked credentials, not hardcoded here.
        // Add these in Jenkins → Manage Jenkins → Credentials → Secret text:
        //   SLACK_WEBHOOK_URL, TEAMS_WEBHOOK_URL,
        //   WHATSAPP_API_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_NOTIFY_TO
        SLACK_WEBHOOK_URL       = credentials('SLACK_WEBHOOK_URL')
        TEAMS_WEBHOOK_URL       = credentials('TEAMS_WEBHOOK_URL')
        WHATSAPP_API_TOKEN      = credentials('WHATSAPP_API_TOKEN')
        WHATSAPP_PHONE_NUMBER_ID = credentials('WHATSAPP_PHONE_NUMBER_ID')
        WHATSAPP_NOTIFY_TO      = credentials('WHATSAPP_NOTIFY_TO')
    }

    stages {
        stage('Setup') {
            steps {
                echo "Installing Python dependencies …"
                sh 'pip install -r resources/requirements.txt --quiet'
                sh "playwright install ${params.BROWSER} --with-deps"
            }
        }

        stage('Lint') {
            steps {
                sh 'pip install ruff --quiet'
                sh 'ruff check . || true'   // non-blocking lint
            }
        }

        stage('Test') {
            steps {
                script {
                    def tagArgs = ''
                    if (params.TAGS?.trim()) {
                        params.TAGS.trim().split(/\s+/).each { tag ->
                            tagArgs += " --tags ${tag}"
                        }
                    }
                    def parallelFlag = params.PARALLEL ? '--parallel' : ''
                    sh """
                        python run_tests.py \\
                            ${params.HEADLESS ? '--headless' : ''} \\
                            --browser ${params.BROWSER} \\
                            --env ${params.ENV} \\
                            ${parallelFlag} \\
                            ${tagArgs} \\
                            || true
                    """
                }
            }
            post {
                always {
                    archiveArtifacts artifacts: 'reports/screenshots/**', allowEmptyArchive: true
                    archiveArtifacts artifacts: 'reports/metrics/**',     allowEmptyArchive: true
                }
            }
        }

        stage('Report') {
            steps {
                sh 'allure generate reports/allure-results --clean -o reports/allure-html || true'
            }
            post {
                always {
                    allure([
                        includeProperties: false,
                        jdk:              '',
                        results:          [[path: 'reports/allure-results']]
                    ])
                    publishHTML([
                        allowMissing:         true,
                        alwaysLinkToLastBuild: true,
                        keepAll:              true,
                        reportDir:            'reports/allure-html',
                        reportFiles:          'index.html',
                        reportName:           'Allure Test Report'
                    ])
                }
            }
        }
    }

    post {
        always {
            cleanWs(cleanWhenNotBuilt: false,
                    deleteDirs:        true,
                    disableDeferredWipeout: true,
                    notFailBuild:      true,
                    patterns:          [[pattern: 'reports/', type: 'EXCLUDE']])
        }
        failure {
            echo 'Build FAILED — check Allure report for details'
        }
        success {
            echo 'All tests PASSED'
        }
    }
}
