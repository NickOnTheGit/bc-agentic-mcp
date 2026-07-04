namespace Zig.Foundation.FeatureManagement;

using Zig.PropertyValuation;
using Zig.System.FeatureManagement;

enumextension 11234914 FeatureExt extends FeatureSAN
{
    value(11234915; RentIncreaseAmountCapping)
    {
        Caption = 'Rent Increase Amount Capping', Comment = 'DevOps95670';
        Implementation = FeatureV2SAN = RentIncrAmountCappingFeature, FeatureV3SAN = RentIncrAmountCappingFeature;
    }
    value(11234916; PostingMaintenanceJournal)
    {
        Caption = 'Posting Maintenance Journal', Comment = 'DevOps87028';
        Implementation = FeatureV2SAN = PostingMaintenanceJournal, FeatureV3SAN = PostingMaintenanceJournal;
    }
    value(11234917; ChangeNonNetRentWithinProposalRent)
    {
        Caption = 'Change Non Net Rent Within Proposal Rent', Comment = 'DevOps96995';
        Implementation = FeatureV2SAN = ChangeNonNetRentFeature, FeatureV3SAN = ChangeNonNetRentFeature;
    }
    value(11234918; SmartDocumentService)
    {
        Caption = 'Smart Document Service', Comment = 'DevOps98506';
        Implementation = FeatureV2SAN = SmartDocumentServiceFeature, FeatureV3SAN = SmartDocumentServiceFeature;
    }
    value(11234919; MoveExchToOAuth)
    {
        Caption = 'Microsoft Graph Calendar API', Comment = 'DevOps145314';
        Implementation = FeatureV2SAN = MSGraphCalendarAPIFeatureFDN, FeatureV3SAN = MSGraphCalendarAPIFeatureFDN;
    }
    value(11234920; CreatePurchaseQuotesInBulk)
    {
        Caption = 'Purchase Quotes in Bulk', Comment = 'DevOps88119';
        Implementation = FeatureV2SAN = CreatePurchaseQuotesInBulk, FeatureV3SAN = CreatePurchaseQuotesInBulk;
    }
    value(11234921; WordlinkApprovalWorkflow)
    {
        Caption = 'Wordlink Approval Workflow', Comment = 'DevOps122363';
        Implementation = FeatureV2SAN = WordlinkApprovalWorkflow, FeatureV3SAN = WordlinkApprovalWorkflow;
    }
    value(11234922; WordlinkDocumentSigning)
    {
        Caption = 'Wordlink Document Signing', Comment = 'DevOps127937';
        Implementation = FeatureV2SAN = WordlinkDocumentSigningFDN, FeatureV3SAN = WordlinkDocumentSigningFDN;
    }
    value(11234923; DigitallySignRentalContract)
    {
        Caption = 'Digitally Sign Rental Contract', Comment = 'DevOps129381';
        Implementation = FeatureV2SAN = DigitallySignRentContrFeatFDN, FeatureV3SAN = DigitallySignRentContrFeatFDN;
    }
    value(11234924; CWExploitatiemodel)
    {
        Caption = 'Cash Value Exploitation Model', Comment = 'DevOps96737';
        Implementation = FeatureV2SAN = CWExploitatiemodel, FeatureV3SAN = CWExploitatiemodel;
    }
    value(11234925; ABSAttachmentStorage)
    {
        Caption = 'Azure Blob Storage for Attachments', Comment = 'DevOps126964';
        Implementation = FeatureV2SAN = ABSAttachmentStorageFDN, FeatureV3SAN = ABSAttachmentStorageFDN;
    }
    value(11234926; BaseCorrespondenceTypeOnEmail)
    {
        Caption = 'Contact Correspondence Type Based on Email', Comment = 'DevOps115219';
        Implementation = FeatureV2SAN = CorrespondenceTypeFeatureFDN, FeatureV3SAN = CorrespondenceTypeFeatureFDN;
    }
    value(11234927; PropValMobileHomeStand)
    {
        Caption = 'Property Valuations for Mobile Homes and Stands', Comment = 'DevOps141602';
        Implementation = FeatureV2SAN = PropValMobileHomeStandFeatFDN, FeatureV3SAN = PropValMobileHomeStandFeatFDN;
    }
    value(11234928; ABSTemporaryStorage)
    {
        Caption = 'Azure Blob Storage for temporary storage', Comment = 'DevOps143061';
        Implementation = FeatureV2SAN = ABSTemporaryStorageFDN, FeatureV3SAN = ABSTemporaryStorageFDN;
    }
    value(11234931; TakeOverContactDetails)
    {
        Caption = 'Take Over Contact Details', Comment = 'DevOps141500';
        Implementation = FeatureV2SAN = TakeOverContactDetailsFeatFDN, FeatureV3SAN = TakeOverContactDetailsFeatFDN;
    }
    value(11234932; SupportEpv2)
    {
        Caption = 'Support EPV 2.0', Comment = 'DevOps169125';
        Implementation = FeatureV2SAN = SupportEpv2FeatureFDN, FeatureV3SAN = SupportEpv2FeatureFDN;
    }
    value(1123491402; WordMergeViaAzureFunction)
    {
        Caption = 'Word Merge via an Azure Function', Comment = 'DevOps200858';
        Implementation = FeatureV2SAN = WordMergeViaAzureFeatureFDN, FeatureV3SAN = WordMergeViaAzureFeatureFDN;
        ObsoleteReason = 'Old Wordlink is deprecated, use WordMerge via Azure instead.';
        ObsoleteState = Pending;
        ObsoleteTag = '27.2601';
    }
    value(1123491404; UXImprovementsv1FDN)
    {
        Caption = 'UX Improvements V1', Comment = 'DevOps217358';
        Implementation = FeatureV2SAN = UXImprovementsv1FDN, FeatureV3SAN = UXImprovementsv1FDN;
    }
    value(1123491405; HouseholdNameContrPartners)
    {
        Caption = 'Improved Naming of Households and Customers', Comment = 'DevOps217455';
        Implementation = FeatureV2SAN = ImprovHouseholdCustomerNameFDN, FeatureV3SAN = ImprovHouseholdCustomerNameFDN;
    }
    value(1123491406; UXImprovementsv2FDN)
    {
        Caption = 'UX Improvements V2', Comment = 'DevOps217837';
        Implementation = FeatureV2SAN = UXImprovementsv2FDN, FeatureV3SAN = UXImprovementsv2FDN;
    }
    value(1123491407; PropValConformLawAfforRentFDN)
    {
        Caption = 'Property Valuation Conform Law Affordable Rent', Comment = 'DevOps221573';
        Implementation = FeatureV2SAN = PropValConformLawAfforRentFDN, FeatureV3SAN = PropValConformLawAfforRentFDN;
    }
    value(1123491409; UXImprovementsv3FDN)
    {
        Caption = 'UX Improvements V3', Comment = 'DevOps226864';
        Implementation = FeatureV3SAN = UXImprovementsv3FDN;
    }
    value(1123491410; RentPropPayDetCustomer)
    {
        Caption = 'Rental Proposal with Payment Details New Customer', Comment = 'DevOps247155';
        Implementation = FeatureV3SAN = RentPropPayDetCustomerEMP;
    }
    value(1123491411; RegisterFinAdminFeat)
    {
        Caption = 'Improve Registration of Financial Administrators', Comment = 'DevOps248009';
        Implementation = FeatureV3SAN = RegisterFinAdminFeatFDN;
    }
    value(1123491412; RealEstateObjectFeatureFDN)
    {
        Caption = 'Real Estate Objects and Lettable Objects', Comment = 'DevOps256864';
        Implementation = FeatureV3SAN = RealEstateObjectFeatureFDN;
    }
    value(1123491413; AutomaticPostcodeUpdatesFeatureFDN)
    {
        Caption = 'Automatic Postcode Updates', Comment = 'DevOps264826';
        Implementation = FeatureV3SAN = AutoPostcodeUpdatesFeatureFDN;
    }
}