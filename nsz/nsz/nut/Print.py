import sys
import time
import json
from sys import argv
from nsz.ParseArguments import *
from traceback import print_exc

enableInfo = True
enableError = True
enableWarning = True
enableDebug = False
silent = False
# Turning on machine output will convert all levels to JSON.
machineReadableOutput = False
lastProgress = ''
lowVerbose = False

if len(argv) > 1:
	# We must re-parse the command line parameters here because this module
	# is re-imported in multiple modules which resets the variables each import.
	args = ParseArguments.parse()

	# Does the user want machine readable output?
	if (args.machine_readable):
		machineReadableOutput = True
	if (args.low_verbose):
		lowVerbose = True

def info(s, pleaseNoPrint = None):
	if silent or not enableInfo or lowVerbose:
		return
	
	if pleaseNoPrint == None:
		if machineReadableOutput == False:
			sys.stdout.write(s + "\n")
	else:
		if machineReadableOutput == False:
			while pleaseNoPrint.value() > 0:
				time.sleep(0.01)
			pleaseNoPrint.increment()
			sys.stdout.write(s + "\n")
			sys.stdout.flush()
			pleaseNoPrint.decrement()

def error(errorCode, s):
	if silent or not enableError:
		return
	if machineReadableOutput:
		s = json.dumps({"error": s, "errorCode": errorCode, "warning": False})

	sys.stdout.write(s + "\n")

def warning(s):
	if silent or not enableWarning:
		return
	if machineReadableOutput:
		s = json.dumps({"error": False, "warning": s})

	sys.stdout.write(s + "\n")

def debug(s):
	if silent or not enableDebug:
		return
	if machineReadableOutput == False:
		sys.stdout.write(s + "\n")

def status(s, pleaseNoPrint = None):
	if silent:
		return
	if machineReadableOutput:
		s = json.dumps({"status": s, "error": False, "warning": False})
	if pleaseNoPrint == None:
		sys.stdout.write(s + "\n")
	else:
		while pleaseNoPrint.value() > 0:
			time.sleep(0.01)
		pleaseNoPrint.increment()
		sys.stdout.write(s + "\n")
		sys.stdout.flush()
		pleaseNoPrint.decrement()

def exception():
	if machineReadableOutput == False:
		print_exc()

def progress(job, s):
	global lastProgress

	if machineReadableOutput:
		s = json.dumps({"job": job, "data": s, "error": False, "warning": False})

		if s != lastProgress:
			sys.stdout.write(s + "\n")

			lastProgress = s
