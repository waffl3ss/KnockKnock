#!/usr/bin/python3

import os,re,sys,json,time,logging,contextlib,subprocess,argparse,urllib3
from pathlib import Path
from alive_progress import alive_bar
from argparse import RawTextHelpFormatter
from selenium import webdriver
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.firefox.options import Options
from webdriver_manager.firefox import GeckoDriverManager
from threading import Thread, Event
import httpx
import asyncio
from concurrent.futures import ThreadPoolExecutor, wait
from time import sleep
from ssh import SSHProxyManager
import random

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

banner = r"""
  _  __                 _    _  __                 _
 | |/ /_ __   ___   ___| | _| |/ /_ __   ___   ___| | __
 | ' /| '_ \ / _ \ / __| |/ / ' /| '_ \ / _ \ / __| |/ /
 | . \| | | | (_) | (__|   <| . \| | | | (_) | (__|   <
 |_|\_\_| |_|\___/ \___|_|\_\_|\_\_| |_|\___/ \___|_|\_\\
    v1.2                                  @waffl3ss"""
print(banner)
print("\n")

parser = argparse.ArgumentParser(formatter_class=RawTextHelpFormatter)
parser.add_argument('--teams', dest='runTeams', required=False, default=False, help="Run the Teams User Enumeration Module", action="store_true")
parser.add_argument('--onedrive', dest='runOneDrive', required=False, default=False, help="Run the One Drive Enumeration Module", action="store_true")
parser.add_argument('-l', dest='teamsLegacy', required=False, default=False, help="Write legacy skype users to a seperate file", action="store_true")
parser.add_argument('-s', dest='teamsStatus', required=False, default=False, help="Write Teams Status for users to a seperate file", action="store_true")
parser.add_argument('-i', dest='inputList', type=argparse.FileType('r'), required=True, default='', help="Input file with newline-seperated users to check")
parser.add_argument('-o', dest='outputfile', type=str, required=False, default='', help="Write output to file")
parser.add_argument('-d', dest='targetDomain', type=str, required=True, default='', help="Domain to target")
parser.add_argument('-t', dest='teamsToken', required=False, default='', help="Teams Token, either file, string, or 'proxy' for interactive Firefox")
parser.add_argument('--ssh', dest='sshLogins', required=False, nargs="*", default="", help="SSH proxies to use; format: 'USER1@IP1 USER2@IP2 ...'")
parser.add_argument('--sshKey', dest='sshLoginsKey', type=str, required=False, default="", help="Private SSH keyfile for SSH proxies")
parser.add_argument('--threads', dest='maxThreads', required=False, type=int, default=3, help="Number of threads to use in the Teams User Enumeration (default = 3)")
parser.add_argument('--timeout', dest='timeout', required=False, type=int, default=None, help="Timeout (secs) for each web request (default = none)")
parser.add_argument('--max-connections-thread', dest='max_connections_thread', required=False, type=int, default=100, help="Maximum connections per thread (default = 100)")
parser.add_argument('--max-retries', dest='max_retries', required=False, type=int, default=1, help="Maximum number of retries per username (default = 1)")
parser.add_argument('-v', dest='verboseMode', required=False, default=False, help="Show verbose output", action="store_true")
args = parser.parse_args()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)

if args.verboseMode:
	logger.setLevel(logging.DEBUG)
 
if not args.runTeams and not args.runOneDrive:
    logger.error("You must select one enumeration module, Teams or OneDrive... Exiting...")
    sys.exit()

if args.runTeams and args.teamsToken == '':
    logger.error("Teams Bearer Token required for Teams enumeration, Exiting...")
    sys.exit()

if args.teamsLegacy and args.outputfile == '':
    logger.error("Teams Legacy Output requires the output file option (-o). Exiting...")
    sys.exit()

if args.teamsStatus and args.outputfile == '':
    logger.error("Teams Status Output requires the output file option (-o). Exiting...")
    sys.exit()

MITMPROXY_PORT=8000
USERNAMES_BATCHSIZE = 1000
URL_TEAMS_ENUM = "https://teams.microsoft.com/api/mt/emea/beta/users/"
CLIENT_VERSION_TEAMS = "27/1.0.0.2021011237"
URL_PRESENCE_TEAMS = "https://presence.teams.microsoft.com/v1/presence/getpresence/"

sshProxyManager: SSHProxyManager = None
validNames = set()
legacyNames = set()
statusNames = set()
outfile = None
legOut = None
statusOut = None
allDone = False

# Periodically flushes all files; done to get partial results even if script throws errors and exits
def flushFileBuffersPeriodically():
    global outfile
    global legOut
    global statusOut
    global allDone

    while not allDone:
        sleep(10)
        for fileToFlush in [
            outfile,
            legOut,
            statusOut
        ]:
            if fileToFlush is not None:
                fileToFlush.flush()
    for fileToFlush in [
            outfile,
            legOut,
            statusOut
        ]:
            if fileToFlush is not None:
                fileToFlush.close()

async def OneDriveEnumeratorHandlerAsync(usernameToTry, targetTenant, client, bar):
    for _ in range(0, args.max_retries):
        try:
            logger.debug(" [V] Testing user %s" % usernameToTry)

            url = "https://" + targetTenant + "-my.sharepoint.com/personal/" + usernameToTry.replace(".","_") + "_" + args.targetDomain.replace(".","_") + "/_layouts/15/onedrive.aspx"
            userRequest = await client.get(
                url=url
            )
            if userRequest.status_code in [200, 401, 403, 302]:
                logger.info(" [+] " + usernameToTry + "@" + str(args.targetDomain))
                validNames.add(usernameToTry)
                outfile.write(usernameToTry + "@" + args.targetDomain + "\n")
            else:
                logger.debug(" [-] " + usernameToTry + "@" + str(args.targetDomain))
            break
        except httpx.ReadError:
            await asyncio.sleep(10.0)
        except httpx.ConnectError as e:
            logger.error("Failed to connect to Tenant's OneDrive; most probably it does not exist")
            logger.debug("[V] " + str(e))
            await asyncio.sleep(10.0)
        except Exception as e:
            logger.debug("[V] " + str(e))
            break
    bar()

async def OneDriveEnumerator(targetTenant, bar):
    global outfile
    global legOut
    global statusOut
    global allDone
    
    httpxAsyncClients = []
    limits = httpx.Limits(max_connections=args.max_connections_thread, max_keepalive_connections=args.max_connections_thread)
    timeout = httpx.Timeout(connect=args.timeout, read=None, write=None, pool=None)
    if sshProxyManager is None:
        httpxAsyncClients.append(
                httpx.AsyncClient(verify=False, timeout=timeout, limits=limits)
            )
    else:
        for proxy in sshProxyManager.proxies:
            httpxAsyncClients.append(
                httpx.AsyncClient(verify=False, timeout=timeout, limits=limits, proxy=proxy)
            )
    
    try:
        while True:
            tasks = []
            usernamesToTry = []

            for _ in range(0, USERNAMES_BATCHSIZE):
                usernameToTry = args.inputList.readline().strip().split("@")[0].lower()
                if usernameToTry == "":
                    break
                usernamesToTry.append(usernameToTry)

            if len(usernamesToTry) == 0:
                break
            for usernameToTry in usernamesToTry:
                if usernameToTry == "":
                    continue
                if usernameToTry in validNames: # Skip names that OneDrive enumeration has already found
                    bar()
                    continue
                tasks.append(
                    OneDriveEnumeratorHandlerAsync(
                        usernameToTry=usernameToTry,
                        targetTenant=targetTenant,
                        client=httpxAsyncClients[0] if len(httpxAsyncClients) == 1 else random.choice(httpxAsyncClients),
                        bar=bar
                        )
                )
            
            for taskResult in asyncio.as_completed(tasks):
                await taskResult
    except Exception as e:
        logger.error("[V] " + str(e))

async def TeamsGetPresence(mri, bearer):
    global URL_PRESENCE_TEAMS
    global sshProxyManager

    initHeaders = {
        "x-ms-client-version": "CLIENT_VERSION",
        "Authorization": "Bearer " + bearer,
        "Content-Type": "application/json",
    }   

    json_data = json.dumps([{"mri": mri}])

    try:
        limits = httpx.Limits(max_connections=args.max_connections_thread, max_keepalive_connections=args.max_connections_thread)
        timeout = httpx.Timeout(connect=args.timeout, read=args.timeout, write=args.timeout, pool=None)
        async with httpx.AsyncClient(verify=False, timeout=timeout, limits=limits, proxy=sshProxyManager.get_random_proxy()) as client:
            response = client.post(URL_PRESENCE_TEAMS, data=json_data, headers=initHeaders)
            response.raise_for_status()
    except Exception as e:
        logger.error(f"Error on response. [ERROR] - {e}")
        return None, None, None

    try:
        status = response.json()

        try:
            availability = status[0]['presence']['availability']
        except (KeyError, IndexError, TypeError):
            availability = None

        try:
            device_type = status[0]['presence']['deviceType']
        except (KeyError, IndexError, TypeError):
            device_type = None

        try:
            out_of_office_note = str(status[0]['presence'].get('calendarData', {}).get('outOfOfficeNote', {}).get('message'))
        except (KeyError, IndexError, TypeError):
            out_of_office_note = None
        
        return availability, device_type, out_of_office_note
    
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.error(f"Error parsing response JSON [ERROR] - {e}")
        return None, None, None


async def TeamsEnumeratorHandlerAsync(bar, theToken, client, usernameToTry: str, initHeaders: dict):
    global URL_TEAMS_ENUM

    for _ in range(0, args.max_retries):
        try:
            logger.debug(" [V] Testing user %s" % usernameToTry)

            initRequest = await client.get(
                url=f"{URL_TEAMS_ENUM}{usernameToTry}@{args.targetDomain}/externalsearch?includeTFLUsers=false",
                headers=initHeaders
            )
            if initRequest.status_code == 403:
                logger.info(" [+] %s" % usernameToTry)
                if usernameToTry not in validNames:
                    validNames.add(usernameToTry)
                    outfile.write(usernameToTry + "@" + args.targetDomain + "\n")
            elif initRequest.status_code == 404:
                logger.debug(" Error with username - %s" % str(usernameToTry))
            elif initRequest.status_code == 200:
                statusLevel = json.loads(initRequest.text)
                if statusLevel:
                    if "skypeId" in statusLevel[0]:
                        logger.info(" [+] %s -- Legacy Skype Detected" % usernameToTry)
                        if usernameToTry not in validNames:
                            validNames.add(usernameToTry)
                            outfile.write(usernameToTry + "@" + args.targetDomain + "\n")
                        if usernameToTry not in legacyNames:
                            legacyNames.add(usernameToTry)
                            legOut.write(usernameToTry + "@" + args.targetDomain + "\n")
                        logger.debug(json.dumps(statusLevel, indent=2))
                    else:
                        if not args.teamsStatus:
                            logger.info(" [+] %s" % usernameToTry)
                            if usernameToTry not in validNames:
                                validNames.add(usernameToTry)
                                outfile.write(usernameToTry + "@" + args.targetDomain + "\n")
                            logger.debug(json.dumps(statusLevel, indent=2))
                    if args.teamsStatus:
                        mriStatus = statusLevel[0].get("mri")
                        availability, device_type, out_of_office_note = await TeamsGetPresence(mriStatus, theToken)
                        if out_of_office_note is None:
                            logger.info(f" [+] %s -- %s -- %s" % (usernameToTry, availability, device_type))
                            status = f"{usernameToTry} -- {availability} -- {device_type}"
                            if status not in statusNames:
                                statusNames.add(status)
                                statusOut.write(status + "\n")
                        if out_of_office_note is not None:
                            logger.info(" [+] %s -- %s -- %s -- %s" % (usernameToTry, availability, device_type, repr(out_of_office_note)))
                            status = f"{usernameToTry} -- {availability} -- {device_type} -- {repr(out_of_office_note)}"
                            if status not in statusNames:
                                statusNames.add(status)
                                statusOut.write(status + "\n")
                        if usernameToTry not in validNames:
                            validNames.add(usernameToTry)
                            outfile.write(usernameToTry + "@" + args.targetDomain + "\n")
                else:
                    logger.debug(" [-] %s" % usernameToTry)
            elif initRequest.status_code == 401:
                logger.error(" Error with Teams Auth Token... \n\tShutting down threads and Exiting")
                sys.exit()
            break
        except httpx.ReadError:
            await asyncio.sleep(10.0)
        except httpx.ConnectError:
            await asyncio.sleep(10.0)
        except Exception as e:
            logger.debug("[V] " + str(e))
            break
    bar()

async def TeamsEnumerator(theToken, bar):
    global outfile
    global legOut
    global statusOut
    global validNames
    global USERNAMES_BATCHSIZE
    global allDone
    global sshProxyManager
    
    try:
        httpxAsyncClients = []
        limits = httpx.Limits(max_connections=args.max_connections_thread, max_keepalive_connections=args.max_connections_thread)
        timeout = httpx.Timeout(connect=args.timeout, read=None, write=None, pool=None)
        if sshProxyManager is None:
            httpxAsyncClients.append(
                    httpx.AsyncClient(verify=False, timeout=timeout, limits=limits)
                )
        else:
            for proxy in sshProxyManager.proxies:
                httpxAsyncClients.append(
                    httpx.AsyncClient(verify=False, timeout=timeout, limits=limits, proxy=proxy)
                )
        
        initHeaders = {
            "Host": "teams.microsoft.com",
            "Authorization": "Bearer " + theToken.strip(),
            "X-Ms-Client-Version": CLIENT_VERSION_TEAMS,
        }
        if args.teamsStatus:
            logger.info(" Username -- Availability -- Device Type -- Out of Office Note\n")
        
        while True:
            tasks = []
            usernamesToTry = []

            for _ in range(0, USERNAMES_BATCHSIZE):
                usernameToTry = args.inputList.readline().strip().split("@")[0].lower()
                if usernameToTry == "":
                    break
                usernamesToTry.append(usernameToTry)
            
            if len(usernamesToTry) == 0:
                break

            for usernameToTry in usernamesToTry:
                if usernameToTry == "":
                    continue

                if usernameToTry in validNames: # Skip names that OneDrive enumeration has already found
                    bar()
                    continue

                tasks.append(
                    TeamsEnumeratorHandlerAsync(
                        bar=bar,
                        theToken=theToken,
                        client=httpxAsyncClients[0] if len(httpxAsyncClients) == 1 else random.choice(httpxAsyncClients),
                        usernameToTry=usernameToTry,
                        initHeaders=initHeaders
                    )
                )

            for taskResult in asyncio.as_completed(tasks):
                await taskResult

    except Exception as e:
        logger.error(" [V] " + str(e))

def start_mitmproxy(debug, exit_event):
    mitmproxy_script = os.path.join(os.getcwd(), "mitmproxy_addon.py")

    with open(mitmproxy_script, "w") as f:
        f.write('''
from mitmproxy import http
import os

def response(flow: http.HTTPFlow):
    if "/oauth2/v2.0/token?client-request-id=" in flow.request.path:
        if flow.response and b"skype" in flow.response.content:
            try:
                token_json = flow.response.json()
                access_token = token_json.get("access_token")
                if access_token:
                    with open("token.txt", "w") as token_file:
                        token_file.write("Bearer " + access_token)
                    os._exit(0)
            except Exception as e:
                pass
''')

    def run_mitmproxy():
        cmd = [
            "mitmdump",
            "-s", mitmproxy_script,
            "--mode", "regular",
            "--listen-port", str(MITMPROXY_PORT),
            "--ssl-insecure",
            "--set", "termlog_verbosity=error"
        ]
        if not debug:
            cmd.extend([">", "/dev/null", "2>&1"])
        logger.info("Starting mitmproxy...")
        subprocess.run(cmd)
        exit_event.set()

    thread = Thread(target=run_mitmproxy)
    thread.daemon = True
    thread.start()
    time.sleep(3)

def setup_firefox_options():
    options = Options()
    options.set_preference("security.enterprise_roots.enabled", True)
    options.set_preference("network.proxy.type", 1)
    options.set_preference("network.proxy.http", "localhost")
    options.set_preference("network.proxy.http_port", MITMPROXY_PORT)
    options.set_preference("network.proxy.ssl", "localhost")
    options.set_preference("network.proxy.ssl_port", MITMPROXY_PORT)
    options.set_preference("network.cookie.cookieBehavior", 0)
    options.set_preference("dom.security.https_only_mode", False)
    options.set_preference("privacy.trackingprotection.enabled", False)
    return options

@contextlib.contextmanager
def suppress_stdout_stderr():
    with open(os.devnull, 'w') as devnull:
        with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
            yield

def start_firefox(options):
    logging.getLogger("WDM").setLevel(logging.CRITICAL)
    with suppress_stdout_stderr():
        service = Service(GeckoDriverManager().install())
    driver = webdriver.Firefox(service=service, options=options)
    return driver

def OneDriveGetTenantName(target_domain):
    httpxSyncClients = []
    limits = httpx.Limits(max_connections=args.max_connections_thread, max_keepalive_connections=args.max_connections_thread)
    timeout = httpx.Timeout(connect=args.timeout, read=None, write=None, pool=None)
    if sshProxyManager is None:
        httpxSyncClients.append(
                httpx.Client(verify=False, timeout=timeout, limits=limits)
            )
    else:
        for proxy in sshProxyManager.proxies:
            httpxSyncClients.append(
                httpx.Client(verify=False, timeout=timeout, limits=limits, proxy=proxy)
            )

    client = httpxSyncClients[0] if len(httpxSyncClients) == 1 else random.choice(httpxSyncClients)
    logger.debug(" [V] Method 1: SharePoint Discovery...")
    try:
        sharepoint_url = f"https://{target_domain.split('.')[0]}-my.sharepoint.com"
        response = client.get(sharepoint_url, timeout=args.timeout, follow_redirects=False)

        if response.status_code == 302:
            location = response.headers.get('location', '')
            logger.debug(f" [V] SharePoint redirect: {location}")

            tenant_match = re.search(r'https://([^-]+)-my\.sharepoint\.com', location)
            if tenant_match:
                tenant_name = tenant_match.group(1)
                logger.debug(f" [V] SUCCESS: Found tenant via SharePoint: {tenant_name}")
                return tenant_name
            else:
                logger.debug(" [V] FAIL: Could not extract tenant from SharePoint redirect")
        else:
            logger.debug(f" [V] FAIL: SharePoint response status: {response.status_code}")

    except Exception as e:
        logger.debug(f" [V] FAIL: SharePoint discovery failed: {e}")

    logger.debug(" [V] Method 2: Pattern Probing (backup)...")
    common_patterns = [
        target_domain.split('.')[0],
        target_domain.split('.')[0].replace('-', ''),
        target_domain.replace('.com', '').replace('.', ''),
        target_domain.replace('.', ''),
    ]

    for i, pattern in enumerate(common_patterns):
        logger.debug(f" [V] Testing pattern {i+1}: {pattern}")
        try:
            test_url = f"https://{pattern}-my.sharepoint.com"
            test_response = client.get(test_url, timeout=args.timeout, allow_redirects=False)

            if test_response.status_code in [302, 200, 401, 403]:
                logger.debug(f" [V] SUCCESS: Found working tenant pattern: {pattern} (HTTP {test_response.status_code})")
                return pattern
            else:
                logger.debug(f" [V] FAIL: Pattern {pattern} returned HTTP {test_response.status_code}")

        except Exception as e:
            logger.debug(f" [V] ERROR: Pattern {pattern} failed: {e}")
            continue

    fallback_tenant = target_domain.split('.')[0]
    logger.debug(f" [V] FALLBACK: Using domain-based tenant: {fallback_tenant}")
    client.close()
    return fallback_tenant

def getNumOfLinesInFile(f):
    numOfLines = 0
    for line in f:
        if line.strip() == "":
            continue
        numOfLines += 1
    f.seek(0)
    return numOfLines

def main():
    global bar
    global bar2
    global outfile
    global legOut
    global statusOut
    global allDone
    global sshProxyManager

    ##############################
    ## Setup SSH proxies if needed
    ##############################
    if len(args.sshLogins) != 0:
        ssh_configs = [
            {
                "username": sshLogin.split("@")[0],
                "private_key_path": args.sshLoginsKey,
                "ip": sshLogin.split("@")[1]
            } for sshLogin in args.sshLogins
        ]

        sshProxyManager = SSHProxyManager(ssh_configs=ssh_configs)
        sshProxyManager.start_tunnels()
        sshProxyManager.test_tunnels()

    #####################
    ## Setup output files
    #####################
    if args.outputfile != '':
        # Output file
        if Path.exists(Path(args.outputfile)):
            overwriteOutFileChoice = input(" [!] Output File exists, append? [Y/n] ")
            outfile = open(args.outputfile, "a" if overwriteOutFileChoice in ["y", "Y", ""] else "w")
        else:
            outfile = open(args.outputfile, "w")

        # Teams legacy
        if args.teamsLegacy:
            legacyOutFile = "Legacy_" + str(args.outputfile)
            if Path.exists(Path(legacyOutFile)):
                legOverwriteChoice = input("[!] Legacy Output File exists, append? [Y/n] ")
                legOut = open(legacyOutFile, "a" if legOverwriteChoice in ["y", "Y", ""] else "w")
            else:
                legOut = open(args.outputfile, "w")
        else:
            logger.info(" No legacy skype users will be identified")

        # Teams status
        if args.teamsStatus:
            if args.outputfile != '':
                statusOutFile = "Status_" + str(args.outputfile)

                if Path.exists(Path(statusOutFile)):
                    statusOverwriteChoice = input(" [!] Status Output File exists, append? [Y/n] ")
                    statusOut = open(statusOutFile, "a" if statusOverwriteChoice in ["y", "Y", ""] else "w")

                    statusOut.write("Username -- Availability -- Device Type -- Out of Office Note\n")
                else:
                    statusOut = open(args.outputfile, "w")

    ###########
    ## OneDrive
    ###########
    if args.runOneDrive:
        try:
            logger.info(" Running OneDrive Enumeration")

            try:
                logger.debug(" [V] Discovering tenant for target domain")
                targetTenant = OneDriveGetTenantName(args.targetDomain)
                if not targetTenant:
                    logger.error(" Error retrieving tenant for target, Exiting...")
                    sys.exit()
            except Exception as e:
                logger.error(" Error retrieving tenant for target, Exiting...")
                logger.error(" [V] " + str(e))
                sys.exit()

            logger.debug(" [V] Using target tenant %s" % targetTenant)
            logger.debug(" [V] Running OneDrive Enumeration")

            with alive_bar(getNumOfLinesInFile(args.inputList), title="Enumerating Teams Users", enrich_print=False) as bar:
                with ThreadPoolExecutor(max_workers=args.maxThreads + 1) as threadPoolExecutor:
                    allDone = False
                    tasks = []

                    threadPoolExecutor.submit(flushFileBuffersPeriodically)

                    for threadNum in range(0, args.maxThreads):
                        tasks.append(
                            threadPoolExecutor.submit(
                            asyncio.run,
                            OneDriveEnumerator(
                                targetTenant,
                                bar
                                )
                            )
                        )
                    wait(tasks)
                    allDone = True

        except Exception as e:
            logger.error(" Error running OneDrive Enumeration")
            logger.error(" " + str(e))

    ########
    ## Teams
    ########
    if args.runTeams:
        try:
            # Take Bearer token from file mentioned in parameter
            if len(args.teamsToken) < 150 and Path(args.teamsToken).is_file():
                tokenFile = open(args.teamsToken, 'r')
                theToken = str(tokenFile.read())
                if "Bearer" in theToken:
                    theToken = theToken.replace("Bearer%3D","").replace("%26Origin%3Dhttps%3A%2F%2Fteams.microsoft.com","").replace("%26origin%3Dhttps%3A%2F%2Fteams.microsoft.com","").replace("Bearer ","").strip()
                tokenFile.close()

            # Setup Firefox to interactively retrieve the token
            elif args.teamsToken == 'proxy':
                exit_event = Event()
                start_mitmproxy(args.verboseMode, exit_event)
                options = setup_firefox_options()
                driver = start_firefox(options)

                try:
                    logger.info("Opening Teams in Firefox...")
                    driver.get("https://teams.microsoft.com")
                    logger.info("Waiting for authorization token...")
                    exit_event.wait()
                    logger.info("Token captured to token.txt")
                except Exception as e:
                    logger.error(f"Error: {e}")
                finally:
                    logger.info("Closing Firefox and stopping mitmproxy...")
                    driver.quit()
                    subprocess.run(["pkill", "mitmdump"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    os.remove("mitmproxy_addon.py")

                    tokenFile = open("token.txt", 'r')
                    theToken = str(tokenFile.read())
                    theToken = theToken.replace("Bearer ","")
                    tokenFile.close()

            # Take Bearer token from parameter
            else:
                theToken = str(args.teamsToken)
                if "Bearer" in theToken:
                    theToken = theToken.replace("Bearer%3D","").replace("%26Origin%3Dhttps%3A%2F%2Fteams.microsoft.com","").replace("%26origin%3Dhttps%3A%2F%2Fteams.microsoft.com","").replace("Bearer ","").strip()

            logger.info(" Running Teams User Enumeration")
            args.inputList.seek(0)
            
            with alive_bar(getNumOfLinesInFile(args.inputList), title="Enumerating Teams Users", enrich_print=False) as bar2:
                    with ThreadPoolExecutor(max_workers=args.maxThreads + 1) as threadPoolExecutor:
                        allDone = False
                        tasks = []

                        threadPoolExecutor.submit(flushFileBuffersPeriodically)

                        for threadNum in range(0, args.maxThreads):
                            tasks.append(
                                    threadPoolExecutor.submit(
                                    asyncio.run,
                                    TeamsEnumerator(
                                        theToken,
                                        bar2
                                    )
                                )
                            )
                        wait(tasks)
                        allDone = True
                    
        except Exception as e:
            logger.error(" Error running Teams Enumeration")
            logger.error(" " + str(e))

    #############################
    ## Stop results output thread
    #############################
    allDone = True

    ###################
    ## Stop SSH proxies
    ###################
    if sshProxyManager is not None:
        sshProxyManager.stop_tunnels()

if __name__ == "__main__":
    main()
