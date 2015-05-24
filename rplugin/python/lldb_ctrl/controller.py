import os, re, sys
import lldb

from vim_ui import UI

class StepType:
  INSTRUCTION = 1
  INSTRUCTION_OVER = 2
  INTO = 3
  OVER = 4
  OUT = 5

class LLController(object):
  """ Handles LLDB events and commands. """

  # Timeout (sec) for waiting on new events. Due to the current single threaded design,
  # we should wait a few seconds for any new event, and update vim buffers. After which,
  # the user will have to manually request an update (using :LLrefresh).
  eventDelay = 2 # FIXME see processPendingEvents()

  def __init__(self, vifx):
    """ Creates the LLDB SBDebugger object and initializes the UI class. """
    self.target = None
    self.process = None
    self.load_dependent_modules = True

    self.dbg = lldb.SBDebugger.Create()
    self.command_interpreter = self.dbg.GetCommandInterpreter()

    self.vifx = vifx
    self.ui = UI(vifx)

  def complete_command(self, arg, line, pos):
    """ Returns a list of viable completions for line, and cursor at pos """

    result = lldb.SBStringList()
    num = self.command_interpreter.HandleCompletion(line, pos, 1, -1, result)

    if num == -1:
      # FIXME: insert completion character... what's a completion character?
      pass
    elif num == -2:
      # FIXME: replace line with result.GetStringAtIndex(0)
      pass

    if result.GetSize() > 0:
      results =  filter(None, [result.GetStringAtIndex(x) for x in range(result.GetSize())])
      return results
    else:
      return []

  def do_step(self, stepType):
    """ Perform a step command.
        FIXME: if the step does not complete in eventDelay seconds, we relinquish control to
               the main thread, after which the user will have to manually request to update UI.
    """
    if not self.process:
      self.vifx.log("No process to step")
      return

    t = self.process.GetSelectedThread()
    if stepType == StepType.INSTRUCTION:
      t.StepInstruction(False)
    if stepType == StepType.INSTRUCTION_OVER:
      t.StepInstruction(True)
    elif stepType == StepType.INTO:
      t.StepInto()
    elif stepType == StepType.OVER:
      t.StepOver()
    elif stepType == StepType.OUT:
      t.StepOut()

    self.processPendingEvents(self.eventDelay, True)

  def do_select(self, command, args):
    """ Like do_command, but suppress output when "select" is the first argument."""
    a = args.split(' ')
    return self.do_command(command, args, "select" != a[0], True)

  def do_process(self, args):
    """ Handle 'process' command.
    """
    if args.startswith("la"): # launch
      if self.process is not None and self.process.IsValid():
        pid = self.process.GetProcessID()
        self.process.Destroy()

      (success, result) = self.get_command_result("process", args)
      self.process = self.target.process
      if not success:
        self.vifx.log("Error during launch: " + str(result))
        return

      # launch succeeded, store pid and add some event listeners
      self.pid = self.process.GetProcessID()
      self.processListener = lldb.SBListener("process_event_listener")
      self.process.GetBroadcaster().AddListener(self.processListener, lldb.SBProcess.eBroadcastBitStateChanged)

      self.vifx.log("%s" % result, 0)
    elif args.startswith("i"): # interrupt
      if not self.process or not self.process.IsValid():
        self.vifx.log("No valid process to interrupt.")
        return
      self.process.SendAsyncInterrupt()
    elif args.startswith("k"): # kill
      if not self.process or not self.process.IsValid():
        self.vifx.log("No valid process to kill.")
        return
      if not self.process.Destroy().Success():
        self.vifx.log("Error during kill: " + str(error))
      else:
        self.vifx.log("Killed process (pid=%d)" % self.pid)
    else:
      self.do_command("process", args)

    self.processPendingEvents(self.eventDelay)

  def do_attach(self, process_name):
    """ Handle process attach.  """
    error = lldb.SBError()

    self.processListener = lldb.SBListener("process_event_listener")
    self.target = self.dbg.CreateTarget('')
    self.process = self.target.AttachToProcessWithName(self.processListener, process_name, False, error)
    if not error.Success():
      self.vifx.log("Error during attach: " + str(error))
      return

    self.pid = self.process.GetProcessID()

    self.vifx.log("Attached to %s (pid=%d)" % (process_name, self.pid), 0)

  def do_detach(self):
    if self.process is not None and self.process.IsValid():
      pid = self.process.GetProcessID()
      self.process.Detach()
      self.processPendingEvents(self.eventDelay)

  def do_target(self, args):
    """ Pass target command to interpreter
    """
    (success, result) = self.get_command_result("target", args)
    if not success:
      self.vifx.log(str(result))
    elif args.startswith('c'): # create
      self.target = self.dbg.GetSelectedTarget()
      self.vifx.log(str(result), 0)
      self.processPendingEvents(self.eventDelay)

  def do_continue(self):
    """ Handle 'contiue' command.
    """
    # FIXME: switch to do_command("continue", ...) to handle -i ignore-count param.
    if not self.process or not self.process.IsValid():
      self.vifx.log("No process to continue")
      return

    if not self.process.Continue().Success():
      self.vifx.log("Error during continue: " + str(error))
    self.processPendingEvents(self.eventDelay)

  def do_breakswitch(self, filepath, line):
    if self.ui.haveBreakpoint(filepath, line):
      bps = self.ui.getBreakpoints(filepath, line)
      args = "delete %s" % " ".join([str(b.GetID()) for b in bps])
      self.ui.deleteBreakpoints(filepath, line)
    else:
      args = "set -f %s -l %d" % (filepath, line)
    self.do_breakpoint(args)

  def do_breakpoint(self, args):
    """ Handle breakpoint command with command interpreter.
    """
    self.do_command("breakpoint", args, True)

  def do_refresh(self):
    """ process pending events and update UI on request """
    status = self.processPendingEvents()

  def do_exit(self):
    self.dbg.Terminate()
    self.dbg = None

  def get_command_result(self, command, command_args):
    """ Run cmd in the command interpreter and returns (success, output) """
    result = lldb.SBCommandReturnObject()
    cmd = "%s %s" % (command, command_args)

    self.command_interpreter.HandleCommand(cmd, result)
    return (result.Succeeded(), result.GetOutput() if result.Succeeded() else result.GetError())

  def do_command(self, command, command_args, print_on_success=True, goto_file=False):
    """ Run cmd in interpreter and print result (success or failure) on the vim status line. """
    (success, output) = self.get_command_result(command, command_args)
    if success:
      self.ui.update(self.target, "", self.get_command_output, goto_file)
      if len(output) > 0 and print_on_success:
        self.vifx.log(output, 0)
    else:
      self.vifx.log(output)

  def get_command_output(self, command, command_args=""):
    """ runs cmd in the command interpreter andreturns (status, result) """
    result = lldb.SBCommandReturnObject()
    cmd = "%s %s" % (command, command_args)
    self.command_interpreter.HandleCommand(cmd, result)
    return (result.Succeeded(), result.GetOutput() if result.Succeeded() else result.GetError())

  def processPendingEvents(self, wait_seconds=0, goto_file=True): # FIXME replace this with a separate thread
    """ Handle any events that are queued from the inferior.
        Blocks for at most wait_seconds, or if wait_seconds == 0,
        process only events that are already queued.
    """

    status = None
    num_events_handled = 0

    if self.process is not None:
      event = lldb.SBEvent()
      old_state = self.process.GetState()
      new_state = None
      done = False
      if old_state == lldb.eStateInvalid or old_state == lldb.eStateExited:
        # Early-exit if we are in 'boring' states
        pass
      else:
        while not done and self.processListener is not None:
          if not self.processListener.PeekAtNextEvent(event):
            if wait_seconds > 0:
              # No events on the queue, but we are allowed to wait for wait_seconds
              # for any events to show up.
              self.processListener.WaitForEvent(wait_seconds, event)
              new_state = lldb.SBProcess.GetStateFromEvent(event)

              num_events_handled += 1

            done = not self.processListener.PeekAtNextEvent(event)
          else:
            # An event is on the queue, process it here.
            self.processListener.GetNextEvent(event)
            new_state = lldb.SBProcess.GetStateFromEvent(event)

            # continue if stopped after attaching
            if old_state == lldb.eStateAttaching and new_state == lldb.eStateStopped:
              self.process.Continue()

            # If needed, perform any event-specific behaviour here
            num_events_handled += 1

    if num_events_handled == 0:
      pass
    else:
      if old_state == new_state:
        status = ""
    self.ui.update(self.target, status, self.get_command_output, goto_file)

