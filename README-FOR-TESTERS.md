# Nod — please try this and tell me what breaks

Nod is a voice assistant that sits at the top of your screen. You say
**"hey Nod"** and it answers, out loud, in a British-butler sort of way.

It is early. Things will go wrong — that is the point of you having it. Twelve
steps below; each says what you should see and what to do if you do not.

**Two honest warnings before you start:**

- Nod **listens to your microphone** the whole time it is running. It only acts
  on what comes after "hey Nod", but the microphone is open.
- Windows will warn you that it does not recognise this program. It is not
  signed, because a certificate costs a few hundred dollars a year. Step 2 says
  what to click.

---

## Setting up

**1. Unzip the folder anywhere.** Desktop is fine. Keep it together —
`Nod.exe` needs the `_internal` folder beside it.

**2. Double-click `Nod.exe`.** Windows says *"Windows protected your PC"*.
Click **More info**, then **Run anyway**.

> If your antivirus quarantines it instead, tell me which antivirus. That is
> genuinely useful and I would rather know.

**3. A setup window appears.** Click **Get a free key** — your browser opens
Google AI Studio. Sign in, click **Create API key**, copy it.

**4. Paste it into Nod and click Test.** You should see *"✓ The key works."*

> Anything else, copy the message exactly. "Google rejected that key" usually
> means part of it was missed when copying.

**5. Choose your microphone, then click Calibrate.** Please do not skip this.
It asks you to be quiet for three seconds, then to say "hey Nod" a few times.
Nod was built on a headset; a laptop's built-in microphone is much quieter, and
if you skip this and Nod never answers, this is why.

**6. Click Save.** A dark bar appears at the top of your screen reading
**AGENT · EARS: SAY 'HEY NOD'**.

> If it says anything else, write down exactly what.

---

## Trying it

**7. Say "Hey Nod."** It should answer *"Sir?"* within about two seconds, in a
male voice with a Filipino accent.

> Nothing? Open the Start Menu and run **Nod Check-up** (see step 12).
> Try a few times before giving up — *how often* it works is more useful to me
> than whether it worked once.

**8. Say "Hey Nod, what's the exchange rate for dollars to pesos?"**
It looks it up and answers out loud.

**9. Say "Hey Nod, play me some music."** A browser opens and music plays.

**10. Now say "Hey Nod, play me some jazz."** — *the first song must stop.*
If you can hear two at once, that is a bug and I want to know.

**11. Say "Hey Nod, stop"** while it is talking. It should go quiet
immediately, mid-sentence. Then "Hey Nod, hide" — the bar disappears;
`Ctrl+Alt+H` brings it back.

---

## Telling me what happened

**12. Run "Nod Check-up" from the Start Menu, click Copy report.**

Send me:

- that report,
- the file `nod.log` — press `Win+R`, paste `%USERPROFILE%\.nod`, press Enter,
- and anything that felt wrong, however vague. *"It was slow"* and *"it kept
  ignoring me"* are useful.

**The thing I most want to know:** how it sounds. The voice is synthetic and
the accent is applied by rule, not recorded from anyone, so it may land oddly.
If a particular word sounds wrong, write that word down — I can tune individual
sounds.

---

## Things worth knowing

| | |
|---|---|
| **Shortcuts** | `Ctrl+Alt+H` hide/show · `Ctrl+Alt+S` stop talking · `Ctrl+Alt+A` mic on/off · `Ctrl+Alt+M` meeting mode |
| **Turning it off** | Close the terminal, or `Ctrl+Alt+A` to stop it listening without quitting |
| **Your data** | Key and settings stay on your PC in `%USERPROFILE%\.nod`. What you say goes to Google's Gemini API to be answered. Nothing comes to me unless you send it. |
| **Meetings** | Off by default. It can join a Google Meet, and it refuses to join unless it has confirmed your camera is off. Leave it alone for now. |
| **Uninstalling** | Delete the folder, and delete `%USERPROFILE%\.nod`. Nothing is installed anywhere else. |

Thank you for trying it.
