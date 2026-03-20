/*
 * The MIT License (MIT)
 *
 * Copyright (c) 2024
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 *
 * This file is part of the TinyUSB stack.
 */

/*
 * Change Logs:
 * By Nov, 2025
 *      a, dcd_mchp_g2.c for AVR DU initial integration with MPLAB-X/VS Code 
 *      b, Misra C 2023 compliant (Required and Mandatory Rules)
 *      c, Tested with both XC8 and AVR GCC 
 */
#include "tusb_option.h"
#include "tusb.h"
#include "device/dcd.h"

#if CFG_TUD_ENABLED && (CFG_TUSB_MCU == OPT_MCU_AVRDU)
#include "mcc_generated_files/system/utils/compiler.h"

typedef struct TU_ATTR_PACKED USB_ENDPOINT_TABLE_struct
{
    register8_t FIFO[CFG_TUD_ENDPPOINT_MAX * 2u]; /**<FIFO Entry*/
    USB_EP_PAIR_t EP[CFG_TUD_ENDPPOINT_MAX];      /**<USB Endpoint Register Pairs*/
    register16_t FRAMENUM;                        /**<Frame Number*/
} USB_ENDPOINT_TABLE_t;

static USB_ENDPOINT_TABLE_t TU_ATTR_ALIGNED(2) endpointTable;
static uint8_t TU_ATTR_ALIGNED(2) setup_buffer[8] = {0}; /* Buffer for endpoint 0 setup */
static bool sof_int_enabled = false;

/* The RMW registers must NOT be written when this flag is set.*/
static inline void TU_ATTR_ALWAYS_INLINE wait_until_rmw_done(void) {
    while((uint8_t)(USB0.INTFLAGSB & (uint8_t)USB_RMWBUSY_bm) != 0U) {
        ;
    }
}

/* 
 * Reset specified Endpoints from epNumBgn to epNumEnd
 * epNumBgn - starting endpoint number, can be 0 ~ (CFG_TUD_ENDPPOINT_MAX-1)
 * epNumEnd - last endpoint number, can be 0 ~ (CFG_TUD_ENDPPOINT_MAX-1)
 * Note, epNumBgn must <= epNumEnd
 */
static bool reset_eps(uint8_t rhport, uint8_t const epNumBgn, uint8_t const epNumEnd) 
{  
    (void) rhport;
    uint8_t i = 0U;
    
    if ((epNumEnd >= (uint8_t)CFG_TUD_ENDPPOINT_MAX) || (epNumBgn >= (uint8_t)CFG_TUD_ENDPPOINT_MAX) || (epNumBgn > epNumEnd)) {
        return false;
    } 
    
    for (i = epNumBgn; i <= epNumEnd; i++) {
        endpointTable.EP[i].OUT.CTRL = 0U;
        endpointTable.EP[i].OUT.STATUS = 0U;
        endpointTable.EP[i].IN.CTRL = 0U;
        endpointTable.EP[i].IN.STATUS = 0U;
    }
    
    return true;
}

static bool configure_ep0_for_setup(uint8_t rhport, uint8_t ep_size) 
{
    (void) rhport;
    
    uint8_t ep0_size = ep_size;
    uint8_t ep0_size_cfg = (uint8_t)USB_BUFSIZE_DEFAULT_BUF64_gc;
    
    if (ep0_size >= 64U) {
        ep0_size_cfg = (uint8_t)USB_BUFSIZE_DEFAULT_BUF64_gc;
    } else
    if (ep0_size >= 32U) {
        ep0_size_cfg = (uint8_t)USB_BUFSIZE_DEFAULT_BUF32_gc;
    } else 
    if (ep0_size >= 16U) {
        ep0_size_cfg = (uint8_t)USB_BUFSIZE_DEFAULT_BUF16_gc;
    } else
    if (ep0_size >= 8U) {
        ep0_size_cfg = (uint8_t)USB_BUFSIZE_DEFAULT_BUF8_gc;
    } else {
        return false;
    }
    
    /* `IN` buffer is used for DATA(IN/OUT) stage. */
    endpointTable.EP[0].IN.STATUS = (uint8_t)USB_BUSNAK_bm;
    endpointTable.EP[0].IN.CTRL = (uint8_t)((uint8_t)USB_TYPE_CONTROL_gc | (uint8_t)USB_MULTIPKT_bm | ep0_size_cfg);
    /* `OUT` buffer is used for SETUP stage (64 B); */
    endpointTable.EP[0].OUT.DATAPTR = (uint16_t)setup_buffer;
    endpointTable.EP[0].OUT.STATUS = (uint8_t)USB_BUSNAK_bm;
    endpointTable.EP[0].OUT.CTRL = (uint8_t)((uint8_t)USB_TYPE_CONTROL_gc | (uint8_t)USB_MULTIPKT_bm | ep0_size_cfg);

    return true;
}

/* 
 * Initialize controller to device mode 
 * Precondition:
 * - Clocks are configured (CLK_PER>=12 MHz)
 * - VUSB is feed by external 3.3 V directly or by internal 3.3V regulator if VDD>=3.9V & SYSCFG.USBVREG==1
 */
bool dcd_init(uint8_t rhport, const tusb_rhport_init_t* rh_init)
{
    (void) rhport;
    
    /* OVF disabled; RESET disabled; RESUME disabled; SOF disabled; STALLED disabled; SUSPEND disabled; UNF disabled; */
    USB0.INTCTRLA = 0x0U;
    /* GNDONE disabled; SETUP disabled; TRNCOMPL disabled; */
    USB0.INTCTRLB = 0x0U;
    /* Always begins with address 0 for USB Enumeration */
    USB0.ADDR = 0x0U; 
    /* Reset FIFO read & write pointer */
    USB0.FIFORP = 0x0U; 
    USB0.FIFOWP = 0x0U; 
    USB0.EPPTR = (uint16_t)endpointTable.EP;
    /* Clear all bus and EP interrupt flags */
    USB0.INTFLAGSA = (uint8_t)((uint8_t)USB_SOF_bm | (uint8_t)USB_SUSPEND_bm | (uint8_t)USB_RESUME_bm | (uint8_t)USB_RESET_bm | (uint8_t)USB_STALLED_bm | (uint8_t)USB_UNF_bm | (uint8_t)USB_OVF_bm);
    USB0.INTFLAGSB = (uint8_t)((uint8_t)USB_TRNCOMPL_bm | (uint8_t)USB_GNDONE_bm | (uint8_t)USB_SETUP_bm);
    /* Reset all endpoints */
    if (false == reset_eps(0U, 0U, CFG_TUD_ENDPPOINT_MAX-1U)){
        return false;
    }
    if (false == configure_ep0_for_setup(0U, (uint8_t)CFG_TUD_ENDPOINT0_SIZE)) {
        return false;
    }
    USB0.CTRLB = 0x0U; 
    USB0.CTRLA = (uint8_t)((uint8_t)USB_ENABLE_bm | (uint8_t)USB_FIFOEN_bm | (uint8_t)((CFG_TUD_ENDPPOINT_MAX - 1U) & 0x0FU));
    
    return true;
}

/* Deinitialize controller, unset device mode.*/
bool dcd_deinit(uint8_t rhport)
{
    (void) rhport;
    
    USB0.CTRLA = 0x0U;
    USB0.CTRLB = 0x0U;      
    
    USB0.EPPTR = 0x0U;
    USB0.ADDR = 0x0U; 
    /* Reset FIFO read & write pointer */
    USB0.FIFORP = 0x0U; 
    USB0.FIFOWP = 0x0U;   
    
    USB0.INTCTRLA = 0x0U;
    USB0.INTCTRLB = 0x0U;

    /* Reset all EPs and Clear all bus and EP interrupt flags */
    (void)reset_eps(0U, 0U, CFG_TUD_ENDPPOINT_MAX-1U);
    USB0.INTFLAGSA = (uint8_t)((uint8_t)USB_SOF_bm | (uint8_t)USB_SUSPEND_bm | (uint8_t)USB_RESUME_bm | (uint8_t)USB_RESET_bm | (uint8_t)USB_STALLED_bm | (uint8_t)USB_UNF_bm | (uint8_t)USB_OVF_bm);
    USB0.INTFLAGSB = (uint8_t)((uint8_t)USB_TRNCOMPL_bm | (uint8_t)USB_GNDONE_bm | (uint8_t)USB_SETUP_bm);
    
    return true;
}

/* Enable device interrupt */
void dcd_int_enable (uint8_t rhport)
{
    (void) rhport;
    
    USB0.INTCTRLA |= (uint8_t)((uint8_t)USB_SUSPEND_bm | (uint8_t)USB_RESUME_bm | (uint8_t)USB_RESET_bm | (uint8_t)(sof_int_enabled ? (uint8_t)USB_SOF_bm : 0U));
    USB0.INTCTRLB |= (uint8_t)((uint8_t)USB_TRNCOMPL_bm | (uint8_t)USB_SETUP_bm);    
}

/* Disable device interrupt */
void dcd_int_disable(uint8_t rhport)
{
    (void) rhport;
    
    USB0.INTCTRLA = 0U;
    USB0.INTCTRLB = 0U;    
}

/* Receive Set Address request, mcu port must also include status IN response */
void dcd_set_address(uint8_t rhport, uint8_t dev_addr)
{
    (void) rhport;
    
    /* Address need be updated after status stage, 
     * So, response with status first before changing device address */
    (void)dcd_edpt_xfer(rhport, tu_edpt_addr(0U, (uint8_t)TUSB_DIR_IN), NULL, 0U);   
}

/* Wake up host */
void dcd_remote_wakeup(uint8_t rhport)
{
    (void) rhport;
}

/* Connect by enabling internal pull-up resistor on D+/D- */
void dcd_connect(uint8_t rhport)
{
    (void) rhport;
    
    USB0.CTRLB |= (uint8_t)USB_ATTACH_bm;
}

/* Disconnect by disabling internal pull-up resistor on D+/D- */
void dcd_disconnect(uint8_t rhport)
{
    (void) rhport;
    
    USB0.CTRLB &= ~(uint8_t)USB_ATTACH_bm;
}

/* Enable/Disable Start-of-frame interrupt. Default is disabled */
void dcd_sof_enable(uint8_t rhport, bool en)
{
    (void) rhport;
    
    sof_int_enabled = en;
    if(sof_int_enabled) {
        USB0.INTCTRLA |= (uint8_t)USB_SOF_bm;
    } else {
        USB0.INTCTRLA &= ~(uint8_t)USB_SOF_bm;
    }    
}

/*
*--------------------------------------------------------------------+
* Endpoint API
*--------------------------------------------------------------------+
*/

/* 
 * Invoked when a control transfer's status stage is complete.
 * May help DCD to prepare for next control transfer, this API is optional. 
 */
void dcd_edpt0_status_complete(uint8_t rhport, tusb_control_request_t const * request)
{
    (void) rhport;
    
    /* Set address here if the completed transaction is SET_ADDRESS */
    if ((uint8_t)((request->bmRequestType_bit.recipient == (uint8_t)TUSB_REQ_RCPT_DEVICE)
        && (request->bmRequestType_bit.type == (uint8_t)TUSB_REQ_TYPE_STANDARD)
        && (request->bRequest == (uint8_t)TUSB_REQ_SET_ADDRESS)) != 0U) {
            USB0.ADDR = (uint8_t) request->wValue;
    }      
}

/* Configure endpoint's registers according to descriptor */
bool dcd_edpt_open            (uint8_t rhport, tusb_desc_endpoint_t const * desc_ep)
{
    (void) rhport;
    
    uint8_t const ep_addr = desc_ep->bEndpointAddress;
    uint8_t const ep_num = tu_edpt_number(ep_addr);
    tusb_dir_t const dir = tu_edpt_dir(ep_addr);
    const uint16_t packet_size = tu_edpt_packet_size(desc_ep);
    uint8_t xfertype = desc_ep->bmAttributes.xfer;

    /* this API is not used for establishing control transfers */
    uint8_t ctrl_val = (xfertype == (uint8_t)TUSB_XFER_ISOCHRONOUS ? (uint8_t)USB_TYPE_ISO_gc : (uint8_t)USB_TYPE_BULKINT_gc) | (uint8_t)USB_MULTIPKT_bm;
    if(xfertype == (uint8_t)TUSB_XFER_ISOCHRONOUS) {
        if(packet_size == 1023U) {
            ctrl_val |= (uint8_t)USB_BUFSIZE_ISO_BUF1023_gc;
        }
        else if(packet_size == 512U) {
            ctrl_val |= (uint8_t)USB_BUFSIZE_ISO_BUF512_gc;
        } 
        else if(packet_size == 256U) {
            ctrl_val |= (uint8_t)USB_BUFSIZE_ISO_BUF256_gc;
        }
        else if(packet_size == 128U) {
            ctrl_val |= (uint8_t)USB_BUFSIZE_ISO_BUF128_gc;
        }
        else if(packet_size == 64U) {
            ctrl_val |= (uint8_t)USB_BUFSIZE_ISO_BUF64_gc;
        }
        else if(packet_size == 32U) {
            ctrl_val |= (uint8_t)USB_BUFSIZE_ISO_BUF32_gc;
        }
        else if(packet_size == 16U) {
            ctrl_val |= (uint8_t)USB_BUFSIZE_ISO_BUF16_gc;
        }
        else if(packet_size == 8U) {
            ctrl_val |= (uint8_t)USB_BUFSIZE_ISO_BUF8_gc;
        }
        else {
            return false;
        }
    } else 
    {
        if(packet_size == 64U) {
            ctrl_val |= (uint8_t)USB_BUFSIZE_DEFAULT_BUF64_gc;
        }
        else if(packet_size == 32U) {
            ctrl_val |= (uint8_t)USB_BUFSIZE_DEFAULT_BUF32_gc;
        }
        else if(packet_size == 16U) {
            ctrl_val |= (uint8_t)USB_BUFSIZE_DEFAULT_BUF16_gc;
        }
        else if(packet_size == 8U) {
            ctrl_val |= (uint8_t)USB_BUFSIZE_DEFAULT_BUF8_gc;
        }
        else {
            return false;
        }
    }
    
    if (ep_num >= CFG_TUD_ENDPPOINT_MAX) {
        return false;
    } 
    
    if(dir == TUSB_DIR_OUT) {
        endpointTable.EP[ep_num].OUT.STATUS = (uint8_t)USB_BUSNAK_bm;
        endpointTable.EP[ep_num].OUT.CTRL = ctrl_val;
    } else {
        endpointTable.EP[ep_num].IN.STATUS = (uint8_t)USB_BUSNAK_bm;
        endpointTable.EP[ep_num].IN.CTRL = ctrl_val;
    }
    
    return true;
}

void dcd_edpt_close(uint8_t rhport, uint8_t ep_addr)
{
    (void) rhport;
       
    uint8_t const ep_num = tu_edpt_number(ep_addr);
    tusb_dir_t const dir = tu_edpt_dir(ep_addr);
    
    if (ep_num >= CFG_TUD_ENDPPOINT_MAX) {
        return;
    }    
    
    if(dir == TUSB_DIR_OUT) {
        endpointTable.EP[ep_num].OUT.CTRL = (uint8_t)USB_TYPE_DISABLE_gc;
        endpointTable.EP[ep_num].OUT.STATUS = 0U;
    } else {
        endpointTable.EP[ep_num].IN.CTRL = (uint8_t)USB_TYPE_DISABLE_gc;
        endpointTable.EP[ep_num].IN.STATUS = 0U;
    }
}

/*
 * Close all non-control endpoints, cancel all pending transfers if any.
*/
void dcd_edpt_close_all       (uint8_t rhport)
{
    (void) rhport;
    
    (void)reset_eps(0U, 1U, (uint8_t)(CFG_TUD_ENDPPOINT_MAX-1U));   
}

/* Submit a transfer, When complete dcd_event_xfer_complete() is invoked to notify the stack */
bool dcd_edpt_xfer            (uint8_t rhport, uint8_t ep_addr, uint8_t * buffer, uint16_t total_bytes)
{    
    (void) rhport;
    uint8_t const ep_num = tu_edpt_number(ep_addr);
    tusb_dir_t const dir = tu_edpt_dir(ep_addr);
    
    if (ep_num >= CFG_TUD_ENDPPOINT_MAX) {
        return false;
    }
   
    if((dir != TUSB_DIR_OUT) || ep_num == 0U) {
        /* IN transaction or control transfer data stage in either direction */
        endpointTable.EP[ep_num].IN.DATAPTR = (uint16_t)buffer;
    } else {
        /* OUT transaction */
        endpointTable.EP[ep_num].OUT.DATAPTR = (uint16_t)buffer;
    } 

    if(dir != TUSB_DIR_OUT) {
        /* IN transaction */
        endpointTable.EP[ep_num].IN.CNT = total_bytes;
        endpointTable.EP[ep_num].IN.MCNT = 0U;
        /* 
         * MISRA C-2023 2.2 Deviation Justification:
         * This call is required by the hardware spec to ensure the RMW registers
         * ,INCLR, must NOT be written when this flag is set.
         */           
        wait_until_rmw_done();
        USB0.STATUS[ep_num].INCLR = (uint8_t)((uint8_t)USB_CRC_bm | (uint8_t)USB_UNFOVF_bm | (uint8_t)USB_TRNCOMPL_bm | (uint8_t)USB_EPSETUP_bm | (uint8_t)USB_STALLED_bm | (uint8_t)USB_BUSNAK_bm);
    } else {
        /* OUT transaction */
        endpointTable.EP[ep_num].OUT.MCNT = total_bytes;
        endpointTable.EP[ep_num].OUT.CNT = 0U;
        /* 
         * MISRA C-2023 2.2 Deviation Justification:
         * This call is required by the hardware spec to ensure the RMW registers
         * ,OUTCLR, must NOT be written when this flag is set.
         */           
        wait_until_rmw_done();
        USB0.STATUS[ep_num].OUTCLR = (uint8_t)((uint8_t)USB_CRC_bm | (uint8_t)USB_UNFOVF_bm | (uint8_t)USB_TRNCOMPL_bm | (uint8_t)USB_EPSETUP_bm | (uint8_t)USB_STALLED_bm | (uint8_t)USB_BUSNAK_bm);
    }
    
    return true;
}

/* Stall endpoint, any queuing transfer should be removed from endpoint */
void dcd_edpt_stall           (uint8_t rhport, uint8_t ep_addr)
{
    (void) rhport;
    
    uint8_t const ep_num = tu_edpt_number(ep_addr);
    tusb_dir_t const dir = tu_edpt_dir(ep_addr);
    
    if (ep_num >= CFG_TUD_ENDPPOINT_MAX) {
        return;
    }
    
    if(dir == TUSB_DIR_OUT) {
        endpointTable.EP[ep_num].OUT.CTRL |= (uint8_t)USB_DOSTALL_bm;
    } else {
        endpointTable.EP[ep_num].IN.CTRL |= (uint8_t)USB_DOSTALL_bm;
    }    
}

/* 
 * clear stall, data toggle is also reset to DATA0
 * This API never calls with control endpoints, since it is auto cleared when receiving setup packet
*/
void dcd_edpt_clear_stall     (uint8_t rhport, uint8_t ep_addr)
{
    (void) rhport;
    
    uint8_t const ep_num = tu_edpt_number(ep_addr);
    tusb_dir_t const dir = tu_edpt_dir(ep_addr);
    
    if (ep_num >= CFG_TUD_ENDPPOINT_MAX) {
        return;
    }
    
    if(dir == TUSB_DIR_OUT) {
        /* 
         * MISRA C-2023 2.2 Deviation Justification:
         * This call is required by the hardware spec to ensure the RMW registers
         * ,OUTCLR, must NOT be written when this flag is set.
         */           
        wait_until_rmw_done();
        USB0.STATUS[ep_num].OUTCLR = (uint8_t)USB_TOGGLE_bm;
        endpointTable.EP[ep_num].OUT.CTRL &= ~(uint8_t)USB_DOSTALL_bm;
    } else {
        /* 
         * MISRA C-2023 2.2 Deviation Justification:
         * This call is required by the hardware spec to ensure the RMW registers
         * ,INCLR, must NOT be written when this flag is set.
         */           
        wait_until_rmw_done();
        USB0.STATUS[ep_num].INCLR = (uint8_t)USB_TOGGLE_bm;
        endpointTable.EP[ep_num].IN.CTRL &= ~(uint8_t)USB_DOSTALL_bm;
    }    
}

/* Interrupt Handlers */
static void dcd_bus_int_handler(void) 
{
    uint8_t intflagsa = USB0.INTFLAGSA;
    
    if ((uint8_t)(intflagsa & (uint8_t)USB_SOF_bm) != 0U) {
        /* SOF; notify tusb */
        USB0.INTFLAGSA = (uint8_t)USB_SOF_bm;
        dcd_event_bus_signal(0U, DCD_EVENT_SOF, true);
    }
    if ((uint8_t)(intflagsa & (uint8_t)USB_RESET_bm) != 0U) {
        /* RESET; reset peripheral and notify tusb */
        USB0.ADDR = 0U;
        USB0.FIFORP = 0U;
        USB0.INTFLAGSA = (uint8_t)((uint8_t)USB_SOF_bm | (uint8_t)USB_SUSPEND_bm | (uint8_t)USB_RESUME_bm | (uint8_t)USB_RESET_bm | (uint8_t)USB_STALLED_bm | (uint8_t)USB_UNF_bm | (uint8_t)USB_OVF_bm);
        USB0.INTFLAGSB = (uint8_t)((uint8_t)USB_TRNCOMPL_bm | (uint8_t)USB_GNDONE_bm | (uint8_t)USB_SETUP_bm);
        (void)reset_eps(0U, 0U, (CFG_TUD_ENDPPOINT_MAX - 1U));
        (void)configure_ep0_for_setup(0U, (uint8_t)CFG_TUD_ENDPOINT0_SIZE);
        /* USB0.CTRLB = 0; */
        USB0.CTRLA = (uint8_t)((uint8_t)USB_ENABLE_bm | (uint8_t)USB_FIFOEN_bm | (uint8_t)((CFG_TUD_ENDPPOINT_MAX - 1U) & 0x0FU));
        dcd_event_bus_signal(0U, DCD_EVENT_BUS_RESET, true);
    }
    if ((uint8_t)(intflagsa & (uint8_t)USB_SUSPEND_bm) != 0U) {
        /* SUSPEND; just notify tusb */
        USB0.INTFLAGSA = (uint8_t)USB_SUSPEND_bm;
        dcd_event_bus_signal(0U, DCD_EVENT_SUSPEND, true);
    }
    if ((uint8_t)(intflagsa & (uint8_t)USB_RESUME_bm) != 0U) {
        /* RESUME; just notify tusb */
        USB0.INTFLAGSA = (uint8_t)USB_RESUME_bm;
        dcd_event_bus_signal(0U, DCD_EVENT_RESUME, true);
    }
}

static void trancompl_handler(void) {
    while ((uint8_t)(USB0.INTFLAGSB & (uint8_t)USB_TRNCOMPL_bm) != 0U) {
        /* Finds FIFO entry by adding (subtracting) the signed read pointer(USB0.FIFORP) to the size of the FIFO */
        uint8_t const fifoEntry = endpointTable.FIFO[(((int8_t)CFG_TUD_ENDPPOINT_MAX * 2) + (int8_t)USB0.FIFORP)];

        uint8_t ep_num = fifoEntry >> USB_EPNUM_gp;
        tusb_dir_t dir_in = (uint8_t)(fifoEntry & (uint8_t)USB_DIR_bm)>0U ? TUSB_DIR_IN : TUSB_DIR_OUT;
        uint8_t ep_addr = tu_edpt_addr(ep_num, (uint8_t)dir_in);
        uint16_t bytes_transferred;
        
        if(TUSB_DIR_IN == dir_in) {
            bytes_transferred = endpointTable.EP[ep_num].IN.CNT;
            /* 
             * MISRA C-2023 2.2 Deviation Justification:
             * This call is required by the hardware spec to ensure the RMW registers
             * ,INCLR, must NOT be written when this flag is set.
             */            
            wait_until_rmw_done();
            USB0.STATUS[ep_num].INCLR = (uint8_t)USB_TRNCOMPL_bm;
        } else {
            bytes_transferred = endpointTable.EP[ep_num].OUT.CNT;
            /* 
             * MISRA C-2023 2.2 Deviation Justification:
             * This call is required by the hardware spec to ensure the RMW registers
             * ,OUTCLR, must NOT be written when this flag is set.
             */            
            wait_until_rmw_done();
            USB0.STATUS[ep_num].OUTCLR = (uint8_t)USB_TRNCOMPL_bm;
        }
        
        dcd_event_xfer_complete(0U, ep_addr, (uint32_t)bytes_transferred, (uint8_t)XFER_RESULT_SUCCESS, true);
    }
}

static void dcd_xfer_int_handler(void) {
    uint8_t intflagsb = USB0.INTFLAGSB;
    
    if((uint8_t)(intflagsb & (uint8_t)USB_SETUP_bm) != 0U) {
        /* SETUP transaction, clear SETUP interrupt flag first */
        USB0.INTFLAGSB = (uint8_t)USB_SETUP_bm;

        dcd_event_setup_received(0U, setup_buffer, true);
        /* 
         * MISRA C-2023 2.2 Deviation Justification:
         * This call is required by the hardware spec to ensure the RMW registers
         * ,OUTCLR, must NOT be written when this flag is set.
         */
        wait_until_rmw_done();
        USB0.STATUS[0].OUTCLR = (uint8_t)USB_EPSETUP_bm;
        /* 
         * MISRA C-2023 2.2 Deviation Justification:
         * This call is required by the hardware spec to ensure the RMW registers
         * ,INCLR, must NOT be written when this flag is set.
         */        
        wait_until_rmw_done();
        USB0.STATUS[0].INCLR = (uint8_t)USB_EPSETUP_bm;

        /* EP0 stall is cleared on a new SETUP. */        
        endpointTable.EP[0].IN.CTRL &= ~(uint8_t)USB_DOSTALL_bm;
        endpointTable.EP[0].OUT.CTRL &= ~(uint8_t)USB_DOSTALL_bm;
    }
    
    if((uint8_t)(intflagsb & (uint8_t)USB_GNDONE_bm) != 0U) {
        /* Dont't care */
        USB0.INTFLAGSB = (uint8_t)USB_GNDONE_bm;
    }
    
    /* TRNCOMPL is cleared when FIFO has been emptied */
    trancompl_handler();    
}

/*
 * MISRA C-2023 Rule 21.2 Deviation Justification:
 *
 * The 'ISR()' macro is a mandatory, toolchain-specific compiler extension
 * (attribute) required by the target hardware/toolchain to correctly
 * generate the interrupt vector table entry for the USB0_BUSEVENT.
 *
 * This non-standard syntax is strictly necessary as the standard C language
 * does not provide a mechanism for defining interrupt service routines.
 * Use of this mechanism is therefore unavoidable for low-level implementation 
 * is validated by the toolchain.
 */
ISR(USB0_BUSEVENT_vect) {
  dcd_bus_int_handler();
}

/*
 * MISRA C-2023 Rule 21.2 Deviation Justification:
 *
 * The 'ISR()' macro is a mandatory, toolchain-specific compiler extension
 * (attribute) required by the target hardware/toolchain to correctly
 * generate the interrupt vector table entry for the USB0_TRNCOMPL.
 *
 * This non-standard syntax is strictly necessary as the standard C language
 * does not provide a mechanism for defining interrupt service routines.
 * Use of this mechanism is therefore unavoidable for low-level implementation 
 * is validated by the toolchain.
 */
ISR(USB0_TRNCOMPL_vect) {
  dcd_xfer_int_handler();
}

#endif
