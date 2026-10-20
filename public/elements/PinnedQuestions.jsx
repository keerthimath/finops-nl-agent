// Side panel listing this user's pinned questions; click one to ask it. Opened by the "Pinned" button.
// props: { user: string }. Pins are written by PinButton.jsx (same localStorage key).
import { Button } from "@/components/ui/button"
import { ArrowDown, ArrowUp, MessageSquare, X } from "lucide-react"
import { useEffect, useState } from "react"

const storageKey = () => `finops-pins:${props.user || "me"}`

function loadPins() {
  try {
    const pins = JSON.parse(window.localStorage.getItem(storageKey()) || "[]")
    return Array.isArray(pins) ? pins.filter((q) => typeof q === "string") : []
  } catch (e) {
    return []
  }
}

function savePins(pins) {
  try {
    window.localStorage.setItem(storageKey(), JSON.stringify(pins))
  } catch (e) {
    // storage blocked: changes last until the page reloads
  }
  window.dispatchEvent(new Event("finops-pins"))
}

export default function PinnedQuestions() {
  const [pins, setPins] = useState([])

  useEffect(() => {
    const sync = () => setPins(loadPins())
    sync()
    window.addEventListener("finops-pins", sync)
    window.addEventListener("storage", sync)
    return () => {
      window.removeEventListener("finops-pins", sync)
      window.removeEventListener("storage", sync)
    }
  }, [])

  const remove = (q) => savePins(pins.filter((p) => p !== q))
  const move = (i, delta) => {
    const next = [...pins]
    const j = i + delta
    if (j < 0 || j >= next.length) return
    ;[next[i], next[j]] = [next[j], next[i]]
    savePins(next)
  }

  if (!pins.length) {
    return (
      <div className="p-2 text-sm text-muted-foreground">
        No pinned questions yet. Use <b>Pin question</b> under an answer to keep a question here; it is saved in
        this browser.
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-1 p-1">
      {pins.map((q, i) => (
        <div key={q} className="group flex items-start gap-1 rounded-md border p-1">
          <Button
            variant="ghost"
            className="h-auto flex-1 justify-start whitespace-normal py-2 text-left font-normal"
            title="Ask this question"
            onClick={() => sendUserMessage(q)}
          >
            <MessageSquare className="mr-2 mt-0.5 h-4 w-4 shrink-0" />
            {q}
          </Button>
          <div className="flex flex-col">
            <Button variant="ghost" size="icon" className="h-6 w-6" title="Move up" onClick={() => move(i, -1)}>
              <ArrowUp className="h-3 w-3" />
            </Button>
            <Button variant="ghost" size="icon" className="h-6 w-6" title="Move down" onClick={() => move(i, 1)}>
              <ArrowDown className="h-3 w-3" />
            </Button>
          </div>
          <Button variant="ghost" size="icon" className="h-6 w-6" title="Unpin" onClick={() => remove(q)}>
            <X className="h-3 w-3" />
          </Button>
        </div>
      ))}
      <p className="px-1 pt-2 text-xs text-muted-foreground">Saved in this browser only.</p>
    </div>
  )
}
