# Nod — please try this and tell me what breaks

Nod is a voice assistant that sits at the top of your screen. You say
**"hey Nod"** and it answers.

It is early. Things will go wrong — that is the point of you having it. There
are twelve steps below; each one says what you should see, and what to do if you
do not see it.

**Before you start, two honest warnings:**

- Nod **listens to your microphone** whenever it is running. It only acts on
  what comes after "hey Nod", but the microphone is open.
- Windows will warn you that it does not recognise this program. It is not
  signed, because a signing certificate costs a few hundred dollars a year.
  Step 2 explains what to click.

---

## Setting up

**1. Unzip the folder anywhere** — Desktop is fine. Keep the whole folder
together; `Nod.exe` needs the `_internal` folder next to it.

**2. Double-click `Nod.exe`.** Windows will say *"Windows protected your PC"*.
Click **More info**, then **Run anyway**.

> If your antivirus quarantines it instead, tell me which antivirus. That is
> useful information and I would rather know.

**3. A setup window appears.** Click **Get a free key**. Your browser opens
Google AI Studio. Sign in, click **Create API key**, and copy it.

**4. Paste the key into Nod and click Test.** You should see *"✓ The key
works."*

> Anything else — copy the message exactly. "Google rejected that key" usually
> means part of the key was missed when copying.

**5. Choose your microphone, then click Calibrate.** It asks you to be quiet for
three seconds, then to say "hey Nod" a few times. This matters more than it
sounds: Nod was built on a headset, and a laptop's built-in microphone is much
quieter. If you skip this and Nod never responds, this is why.

**6. Click Save.** It may download about 145 MB the first time. Then a dark bar
appears at the top of your screen reading **AGENT · EARS: SAY 'HEY NOD'**.

> If it says something else, write down exactly what it says.

---

## Trying it

**7. Say "Hey Nod."** It should answer *"Yes, boss?"* within about two seconds.

> Nothing? Open the Start Menu, run **Nod Check-up**, and see step 12.
> Say it a few times before giving up — telling me *how often* it works is
> more useful than telling me it does not.

**8. Say "Hey Nod, what's the exchange rate for dollars to pesos?"** It should
look it up and answer out loud.

**9. Say "Hey Nod, play me some music."** A browser opens and music plays.

**10. Now say "Hey Nod, play me some jazz."** — *The first song must stop.*
If you can hear two songs at once, that is a bug and I want to know.

**11. Say "Hey Nod, stop."** It should go quiet immediately, mid-sentence.
Then say "Hey Nod, hide" — the bar disappears. `Ctrl+Alt+H` brings it back.

---

## Telling me what happened

**12. Run "Nod Check-up" from the Start Menu and click Copy report.**

Send me:

- that report,
- the file `nod.log` from your user folder — press `Win+R`, paste
  `%USERPROFILE%\.nod` and press Enter,
- and anything that felt wrong, however vague. *"It was slow"* and *"it kept
  ignoring me"* are useful.

---

## Things worth knowing

| | |
|---|---|
| **Keyboard shortcuts** | `Ctrl+Alt+H` hide/show · `Ctrl+Alt+S` stop talking · `Ctrl+Alt+A` microphone on/off · `Ctrl+Alt+M` meeting mode |
| **Turning it off** | Close it from the system tray, or press `Ctrl+Alt+A` to stop it listening without quitting. |
| **Your data** | The key and settings stay on your PC in `%USERPROFILE%\.nod`. What you say to Nod goes to Google's Gemini API to be answered. Nothing goes to me. |
| **Meeting summaries** | Off by default. It needs a separate program (Tesseract) to read your screen. Ignore it for now. |
| **Uninstalling** | Delete the folder, and delete `%USERPROFILE%\.nod`. That is all — nothing is installed elsewhere. |

Thank you for trying it.
